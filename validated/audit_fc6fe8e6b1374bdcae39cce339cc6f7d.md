### Title
ExchangeCapsule.transaction() only enforces non-negative pool balances when the hardened calculation flag is enabled - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java])

### Summary
The Bancor-style exchange balance-update logic in `ExchangeCapsule.transaction()` guards against negative resulting pool balances only when `hardenedCalc` is `true`. Because the on-chain default for the `ALLOW_HARDEN_EXCHANGE_CALCULATION` dynamic parameter is `0` (disabled), a production node runs the legacy, unguarded arithmetic path by default, mirroring the reported bug class: an alternate/legacy calculation path is used in place of the "correct" one and the security check that depends on its output is silently skipped.

### Finding Description
`ExchangeCapsule.transaction()` selects between two `Processor` implementations based on the `hardenedCalc` flag: the legacy `ExchangeProcessor` (floating-point/`Math.pow`-based Bancor formula) when disabled, or `SafeExchangeProcessor` (BigDecimal-based, with `StrictMathWrapper.addExact/subtractExact`) when enabled. [1](#0-0) 

Critically, the check that rejects a resulting negative pool balance is gated on the same flag: [2](#0-1) 

This is structurally identical to the ICHI report's bug class: a legacy/placeholder computation path (there, `UniV3WrappedLibMockup` returning stubbed zero values; here, the un-hardened floating-point `ExchangeProcessor` with no post-condition check) is reachable in production, and the safety check (`maxPositionSize` there; non-negative pool balance here) is bypassed because it is coupled to the "correct" path only.

The default state of this flag is confirmed to be `0` (hardened math disabled) unless a committee proposal activates it: [3](#0-2) 

`SafeExchangeProcessor`'s own tests demonstrate the properties the hardened path enforces that the legacy path does not (non-overshoot, non-negative results, overflow detection): [4](#0-3) 

`ExchangeTransactionActuator` (an actuator directly reachable by any signed `ExchangeTransactionContract` broadcast from an unprivileged account) validates balances and fee up front, but the arithmetic itself, including the negative-balance guard, is delegated to `ExchangeCapsule.transaction()`: [5](#0-4) 

### Impact Explanation
Under the default (non-hardened) configuration, an attacker who can craft `sellTokenQuant`/`expected` values that exploit floating-point/`Math.pow` precision error in the legacy `ExchangeProcessor` could push `newFirstTokenBalance` or `newSecondTokenBalance` negative without the code ever detecting or rejecting it, since the `if (hardenedCalc && ...)` guard never fires. A negative pool balance represents an unbacked/incorrect state of the exchange pair (permanent corruption of exchange accounting), and combined with subsequent trades could allow value to be extracted from the pool beyond what it actually holds — an unbacked balance / theft-of-funds condition reachable by any account issuing a normal `ExchangeTransactionContract` transaction.

### Likelihood Explanation
Reachable by any account with a funded balance and an existing Exchange pair via a single, ordinary `ExchangeTransactionContract` transaction — no special privileges are required. Likelihood of triggering the exact precision-loss edge case depends on picking pool ratios/quantities that expose floating-point/`Math.pow` rounding error in `ExchangeProcessor`, which the existence of the "hardened" rewrite (and its test suite explicitly checking for overshoot/negative-balance defects) indicates is a known, previously-observed real behavior of the legacy processor.

### Recommendation
Make the non-negative balance/overflow safety check unconditional in `ExchangeCapsule.transaction()`, independent of the `hardenedCalc` flag, or set `ALLOW_HARDEN_EXCHANGE_CALCULATION` to be enabled by default / remove the legacy `ExchangeProcessor` path entirely so the safe arithmetic and its guard are always active in production.

### Proof of Concept
1. Ensure `ALLOW_HARDEN_EXCHANGE_CALCULATION == 0` (default state, as shown in `ProposalUtilTest`).
2. Create an `Exchange` pair with a chosen `firstTokenBalance`/`secondTokenBalance` ratio designed to trigger floating-point rounding error in `ExchangeProcessor.exchange()` (via `Math.pow`-based Bancor formula).
3. Submit an `ExchangeTransactionContract` (`ExchangeTransactionActuator`) with a `sellTokenQuant` chosen so the legacy processor computes a `buyTokenQuant` that would drive `newFirstTokenBalance` or `newSecondTokenBalance` negative.
4. Because `hardenedCalc` is `false`, the `if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0))` check at `ExchangeCapsule.java:160-162` never executes, so the transaction succeeds and persists a negative/corrupted exchange balance instead of reverting.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L160-162)
```java
    if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
      throw new ContractValidateException("Exchange balance must be >=0 after transaction");
    }
```

**File:** framework/src/test/java/org/tron/core/actuator/utils/ProposalUtilTest.java (L721-724)
```java
    // 3) current value is 0 (default), proposing 0 again -> rejected
    thrown = assertThrows(ContractValidateException.class, proposeZero);
    assertEquals("[ALLOW_HARDEN_EXCHANGE_CALCULATION] has been set to 0, no need to propose again",
        thrown.getMessage());
```

**File:** framework/src/test/java/org/tron/core/capsule/utils/ExchangeProcessorTest.java (L187-216)
```java
  @Test
  public void testSafeProcessorDivByZeroThrows() {
    // newBalance = balance + quant = -1 + 1 = 0 -> BigDecimal divide by zero
    assertThrows(ArithmeticException.class,
        () -> SafeExchangeProcessor.INSTANCE.exchange(-1L, 100L, 1L));
  }

  @Test
  public void testSafeProcessorAddExactOverflowThrows() {
    // balance + quant = MAX + 1 -> addExact overflow
    assertThrows(ArithmeticException.class,
        () -> SafeExchangeProcessor.INSTANCE.exchange(Long.MAX_VALUE, 1L, 1L));
  }

  @Test
  public void testSafeProcessorNoOvershootForTypicalInputs() {
    // Verify across realistic inputs that hardened result never exceeds buy reserve.
    long[][] data = {
        {100_000_000L, 100_000_000L, 1_000_000L},
        {1_000_000_000L, 1_000_000_000L, 100_000L},
        {1L, 10_140_000_000_000L, 2_897_000_000_000L},
        {903L, 737L, 50L},
    };
    for (long[] row : data) {
      long result = SafeExchangeProcessor.INSTANCE.exchange(row[0], row[1], row[2]);
      Assert.assertTrue("Result must be non-negative", result >= 0);
      Assert.assertTrue("Result must not exceed buy reserve, got " + result + " > " + row[1],
          result <= row[1]);
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L149-182)
```java
    if (!accountStore.has(ownerAddress)) {
      throw new ContractValidateException("account[" + readableOwnerAddress + NOT_EXIST_STR);
    }

    AccountCapsule accountCapsule = accountStore.get(ownerAddress);

    if (accountCapsule.getBalance() < calcFee()) {
      throw new ContractValidateException("No enough balance for exchange transaction fee!");
    }

    ExchangeCapsule exchangeCapsule;
    try {
      exchangeCapsule = Commons.getExchangeStoreFinal(dynamicStore, exchangeStore, exchangeV2Store)
          .get(ByteArray.fromLong(contract.getExchangeId()));
    } catch (ItemNotFoundException ex) {
      throw new ContractValidateException("Exchange[" + contract.getExchangeId()
          + ActuatorConstant.NOT_EXIST_STR);
    }

    byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
    byte[] secondTokenID = exchangeCapsule.getSecondTokenId();
    long firstTokenBalance = exchangeCapsule.getFirstTokenBalance();
    long secondTokenBalance = exchangeCapsule.getSecondTokenBalance();

    byte[] tokenID = contract.getTokenId().toByteArray();
    long tokenQuant = contract.getQuant();
    long tokenExpected = contract.getExpected();

    if (dynamicStore.getAllowSameTokenName() == 1
        && !Arrays.equals(tokenID, TRX_SYMBOL_BYTES)
        && !isNumber(tokenID)) {
      throw new ContractValidateException("token id is not a valid number");
    }
    if (!Arrays.equals(tokenID, firstTokenID) && !Arrays.equals(tokenID, secondTokenID)) {
```
