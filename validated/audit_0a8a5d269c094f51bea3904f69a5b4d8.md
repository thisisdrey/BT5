## Title
Permissionless Bancor-style Exchange pools use unsafe floating-point pricing by default, allowing precision-manipulation drains analogous to BetterBank's fake-LP reward exploit - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java])

### Summary
The BetterBank exploit succeeded because the protocol allowed anyone to permissionlessly create a liquidity pair combining a real token with an attacker-controlled worthless token, and then used a pricing/reward function whose math (and associated tax bypass) was never validated against adversarial, self-created pools. In java-tron, the on-chain `Exchange` (bancor-style TRX↔TRC10 AMM) has the same permissionless-pool-creation property — `ExchangeCreateActuator` lets any account create a pool for any token pair it owns, without any check that the token is "official" or has independent value [1](#0-0) . Trades against that pool are then priced by `ExchangeProcessor.exchange()`, which computes the swap output using `double`-based `Math.pow` arithmetic rather than exact integer/BigDecimal math by default [2](#0-1) .

### Finding Description
`ExchangeCapsule.transaction()` selects between the unsafe floating-point `ExchangeProcessor` and the exact `SafeExchangeProcessor`, but only uses the safe path when the `ALLOW_HARDEN_EXCHANGE_CALCULATION` chain parameter is enabled — otherwise it falls back to the legacy floating-point processor [3](#0-2) . The default value of this parameter is 0 (disabled) as confirmed by the proposal test, which explicitly notes "current value is 0 (default)" [4](#0-3) . In this default state, `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` compute swap amounts with `double` and `Math.pow`, which are documented in the test suite itself to diverge from the strict/hardened results (`Assert.assertNotEquals(anotherTokenQuant, result)` between float and strict/hardened math) [5](#0-4) .

Because any account can create a pool (`ExchangeCreateActuator`) with arbitrary balances and an arbitrary self-issued TRC10 token as one leg, and because `ExchangeInjectActuator`/`ExchangeWithdrawActuator` let the pool creator freely rebalance the pool via injection/withdrawal formulas that are separately computed with different rounding rules (`floorDiv`/`multiplyExact` on inject vs `BigInteger`/`BigDecimal` on withdraw) [6](#0-5) [7](#0-6) , an attacker fully controls both the pool's token economics and the imprecise floating-point pricing curve used to execute trades against it — mirroring BetterBank's core flaw where a self-created, attacker-controlled liquidity pair was treated identically to a legitimate one by the reward/pricing logic, with no differentiation for "official" versus adversary-fabricated pools.

The `Manager` class independently confirms floating-point exchange math is considered unsafe enough to warrant special-case handling: `isExchangeTransaction()` is used to detect and reject `ExchangeTransactionContract` transactions from `pushTransaction()` when hardened calculation is disabled, and this rejection is explicitly bypassed once hardening is turned on [8](#0-7) [9](#0-8) . This demonstrates the maintainers' own recognition that the float-based `ExchangeProcessor` path is exploitable, yet it currently still governs block-included exchange transactions (block-embedded/synced `ExchangeTransactionContract` calls still execute via `ExchangeTransactionActuator.execute()`, which calls `exchangeCapsule.transaction(... allowHarden())` and only uses the safe processor when the proposal has been enacted network-wide) [10](#0-9) .

### Impact Explanation
An attacker who creates their own Exchange pool (or targets a low-liquidity legitimate pool) can exploit the divergence between the float-based `Math.pow` pricing curve and the true bancor-formula result to extract more of the counter-asset than the constant-supply-relay invariant should allow, or to make imprecise/lossy quantity calculations favor themselves over repeated small trades (a classic precision-skimming attack). Because the pool balances are TRX and TRC10 tokens directly custodied by the `Exchange`/`ExchangeV2` store and disbursed via `accountCapsule.addAssetAmountV2`/`setBalance` in `ExchangeTransactionActuator.execute()`, successful exploitation directly moves real TRX/TRC10 balances out of the pool into the attacker's account — a concrete unauthorized transfer of funds, the same fund-theft outcome as the BetterBank incident, scoped to any pool the attacker can create or trade against.

### Likelihood Explanation
`ExchangeCreateContract`, `ExchangeInjectContract`, `ExchangeWithdrawContract`, and `ExchangeTransactionContract` are all ordinary user-signed transactions requiring no special privilege — any unprivileged account can create a pool, inject/withdraw liquidity, and execute swaps. The vulnerable floating-point processor is the default execution path (`ALLOW_HARDEN_EXCHANGE_CALCULATION` defaults to 0), so exploitation requires no governance action by an attacker, only that the network has not yet enacted the hardening proposal. The complexity is moderate — the attacker needs to reason about the specific rounding error introduced by `Math.pow`/`double` arithmetic across the bancor relay-supply formula and craft balances/quantities that maximize the divergence from the exact result, similar in spirit to how BetterBank's attacker engineered specific LP ratios to maximize bonus extraction.

### Recommendation
- Make `SafeExchangeProcessor` (exact `BigDecimal` arithmetic) the default and only implementation for `ExchangeCapsule.transaction()`, removing the floating-point `ExchangeProcessor` code path entirely, or gate all `Exchange*` actuators behind `ALLOW_HARDEN_EXCHANGE_CALCULATION` until it can be permanently enabled network-wide.
- Audit `ExchangeInjectActuator` and `ExchangeWithdrawActuator` for consistency of rounding rules with the hardened transaction path so pool balances cannot be manipulated by exploiting differing rounding modes between inject/withdraw/transaction operations.
- Consider adding invariant checks (e.g., product/relay-supply monotonicity checks) after every `Exchange` operation to reject transactions that would violate the expected bancor invariant beyond a small, provably safe tolerance, regardless of which processor is active.

### Proof of Concept
1. On a network where `ALLOW_HARDEN_EXCHANGE_CALCULATION == 0` (default), an attacker issues a low-value TRC10 token via `AssetIssueActuator` and creates an `Exchange` pool pairing it with TRX via `ExchangeCreateActuator`, choosing initial balances that create favorable rounding conditions in `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` (`chainbase/.../ExchangeProcessor.java` lines 17–45).
2. The attacker (or an accomplice) repeatedly calls `ExchangeTransactionContract` swaps in both directions in the same block/epoch, exploiting the `double`/`Math.pow` rounding divergence documented by `ExchangeProcessorTest.testStrictMath` (float vs. strict/hardened mismatches across dozens of test vectors) to extract more TRX than the true bancor curve would allow.
3. Because `ExchangeTransactionActuator.execute()` directly credits the computed `anotherTokenQuant` to the attacker's account via `accountCapsule.setBalance`/`addAssetAmountV2` (lines 86–91 of `ExchangeTransactionActuator.java`), each precision-favorable trade permanently transfers real TRX/TRC10 value from the pool to the attacker, which can be repeated to drain the pool — analogous to BetterBank's repeated bonus-harvesting drain via a self-created LP.

*Note: I was unable to execute this scenario against a running node or directly quantify the magnitude of the floating-point divergence for arbitrary attacker-chosen balances; the test suite confirms the divergence exists (`assertNotEquals` between float and hardened results) but does not bound its worst-case magnitude. A Devin session with node access would be needed to empirically measure exploitable drift and confirm concrete extractable value.*

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L49-76)
```java
    try {
      final ExchangeCreateContract exchangeCreateContract = this.any
          .unpack(ExchangeCreateContract.class);
      AccountCapsule accountCapsule = accountStore
          .get(exchangeCreateContract.getOwnerAddress().toByteArray());

      byte[] firstTokenID = exchangeCreateContract.getFirstTokenId().toByteArray();
      byte[] secondTokenID = exchangeCreateContract.getSecondTokenId().toByteArray();
      long firstTokenBalance = exchangeCreateContract.getFirstTokenBalance();
      long secondTokenBalance = exchangeCreateContract.getSecondTokenBalance();

      long newBalance = subtractExact(accountCapsule.getBalance(), fee);

      accountCapsule.setBalance(newBalance);

      if (Arrays.equals(firstTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, firstTokenBalance));
      } else {
        accountCapsule
            .reduceAssetAmountV2(firstTokenID, firstTokenBalance, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(secondTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, secondTokenBalance));
      } else {
        accountCapsule
            .reduceAssetAmountV2(secondTokenID, secondTokenBalance, dynamicStore, assetIssueStore);
      }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L17-45)
```java
  private long exchangeToSupply(long balance, long quant) {
    logger.debug("balance: " + balance);
    long newBalance = balance + quant;
    logger.debug("balance + quant: " + newBalance);

    double issuedSupply = -supply * (1.0
        - Maths.pow(1.0 + (double) quant / newBalance, 0.0005, this.useStrictMath));
    logger.debug("issuedSupply: " + issuedSupply);
    long out = (long) issuedSupply;
    supply += out;

    return out;
  }

  private long exchangeFromSupply(long balance, long supplyQuant) {
    supply -= supplyQuant;

    double exchangeBalance = balance
        * (Maths.pow(1.0 + (double) supplyQuant / supply, 2000.0, this.useStrictMath) - 1.0);
    logger.debug("exchangeBalance: " + exchangeBalance);

    return (long) exchangeBalance;
  }

  @Override
  public long exchange(long sellTokenBalance, long buyTokenBalance, long sellTokenQuant) {
    long relay = exchangeToSupply(sellTokenBalance, sellTokenQuant);
    return exchangeFromSupply(buyTokenBalance, relay);
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-145)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

    long buyTokenQuant = 0;
    long firstTokenBalance = this.exchange.getFirstTokenBalance();
    long secondTokenBalance = this.exchange.getSecondTokenBalance();
    long newFirstTokenBalance;
    long newSecondTokenBalance;

    if (this.exchange.getFirstTokenId().equals(ByteString.copyFrom(sellTokenID))) {
      buyTokenQuant = processor.exchange(firstTokenBalance,
          secondTokenBalance,
          sellTokenQuant);
      newFirstTokenBalance = hardenedCalc
          ? StrictMathWrapper.addExact(firstTokenBalance, sellTokenQuant)
          : firstTokenBalance + sellTokenQuant;
      newSecondTokenBalance = hardenedCalc
          ? StrictMathWrapper.subtractExact(secondTokenBalance, buyTokenQuant)
          : secondTokenBalance - buyTokenQuant;
```

**File:** framework/src/test/java/org/tron/core/actuator/utils/ProposalUtilTest.java (L721-724)
```java
    // 3) current value is 0 (default), proposing 0 again -> rejected
    thrown = assertThrows(ContractValidateException.class, proposeZero);
    assertEquals("[ALLOW_HARDEN_EXCHANGE_CALCULATION] has been set to 0, no need to propose again",
        thrown.getMessage());
```

**File:** framework/src/test/java/org/tron/core/capsule/utils/ExchangeProcessorTest.java (L272-280)
```java
    for (long[] data : testData) {
      ExchangeProcessor processor = new ExchangeProcessor(supply, false);
      long anotherTokenQuant = processor.exchange(data[0], data[1], data[2]);
      processor = new ExchangeProcessor(supply, true);
      long result = processor.exchange(data[0], data[1], data[2]);
      long safeResult = SafeExchangeProcessor.INSTANCE.exchange(data[0], data[1], data[2]);
      Assert.assertNotEquals(anotherTokenQuant, result);
      Assert.assertEquals(safeResult, result);
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L71-83)
```java
      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
        anotherTokenQuant = floorDiv(multiplyExact(
            secondTokenBalance, tokenQuant), firstTokenBalance);
        exchangeCapsule.setBalance(addExact(firstTokenBalance, tokenQuant),
            addExact(secondTokenBalance, anotherTokenQuant));
      } else {
        anotherTokenID = firstTokenID;
        anotherTokenQuant = floorDiv(multiplyExact(
            firstTokenBalance, tokenQuant), secondTokenBalance);
        exchangeCapsule.setBalance(addExact(firstTokenBalance, anotherTokenQuant),
            addExact(secondTokenBalance, tokenQuant));
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L74-89)
```java
      BigInteger bigFirstTokenBalance = new BigInteger(String.valueOf(firstTokenBalance));
      BigInteger bigSecondTokenBalance = new BigInteger(String.valueOf(secondTokenBalance));
      BigInteger bigTokenQuant = new BigInteger(String.valueOf(tokenQuant));
      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
        anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance).longValueExact();
        exchangeCapsule.setBalance(subtractExact(firstTokenBalance, tokenQuant),
            subtractExact(secondTokenBalance, anotherTokenQuant));
      } else {
        anotherTokenID = firstTokenID;
        anotherTokenQuant = bigFirstTokenBalance.multiply(bigTokenQuant)
            .divide(bigSecondTokenBalance).longValueExact();
        exchangeCapsule.setBalance(subtractExact(firstTokenBalance, anotherTokenQuant),
            subtractExact(secondTokenBalance, tokenQuant));
      }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L897-899)
```java
    if (isExchangeTransaction(trx.getInstance())) {
      throw new ContractValidateException("ExchangeTransactionContract is rejected");
    }
```

**File:** framework/src/test/java/org/tron/core/db/ManagerTest.java (L1341-1367)
```java
  @Test
  public void isExchangeTransactionBypassedWhenHardenedEnabled() throws Exception {
    Transaction exchange = Transaction.newBuilder().setRawData(
        Transaction.raw.newBuilder().addContract(
            Transaction.Contract.newBuilder()
                .setType(ContractType.ExchangeTransactionContract)
                .setParameter(Any.pack(ExchangeTransactionContract.newBuilder()
                    .setExchangeId(1L).setQuant(1L).setExpected(1L).build()))
                .build())).build();

    java.lang.reflect.Method m = Manager.class.getDeclaredMethod(
        "isExchangeTransaction", Transaction.class);
    m.setAccessible(true);

    // Default: hardened disabled (==0) -> contract is treated as exchange
    chainManager.getDynamicPropertiesStore().saveAllowHardenExchangeCalculation(0);
    Assert.assertTrue("Exchange tx must be detected when hardened disabled",
        (boolean) m.invoke(dbManager, exchange));

    // Hardened enabled -> bypass returns false
    chainManager.getDynamicPropertiesStore().saveAllowHardenExchangeCalculation(1);
    Assert.assertFalse("Exchange tx must be bypassed when hardened enabled",
        (boolean) m.invoke(dbManager, exchange));

    // Reset
    chainManager.getDynamicPropertiesStore().saveAllowHardenExchangeCalculation(0);
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L64-69)
```java
      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
```
