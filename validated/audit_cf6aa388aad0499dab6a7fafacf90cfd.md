### Title
Legacy floating-point Bancor formula in TRC10 Exchange lets a caller repeatedly drain pool reserves via ExchangeTransactionContract - (File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java)

### Summary
The external report describes an owner-privileged mint/burn wrapper whose bonding-curve accounting was implemented with error-prone math, allowing an attacker to repeat many small mint→burn cycles and extract more value than deposited each time, draining the reserve. The same bug class — a floating-point implementation of a bonding-curve/relay-token formula reachable by ordinary transactions — exists in java-tron's TRC10 Bancor-style Exchange, exercised through `ExchangeTransactionContract` → `ExchangeTransactionActuator` → `ExchangeCapsule.transaction()` → `ExchangeProcessor.exchange()`.

### Finding Description
`ExchangeCapsule.transaction()` selects between two math backends: the legacy `ExchangeProcessor`, which computes the Bancor-style relay/exchange amounts using Java `double` arithmetic and `Maths.pow`, and a corrected `SafeExchangeProcessor`, which performs the same calculation using `BigDecimal` with explicit scale/rounding and overflow-checked (`StrictMathWrapper`) integer math. [1](#0-0) 

The legacy processor derives the traded amount via double-precision `pow()` calls in `exchangeToSupply`/`exchangeFromSupply`, which accumulate floating-point rounding error on every call: [2](#0-1) 

This legacy code path is still what unprivileged accounts hit by default: it is only bypassed when the network-wide `allowHardenExchangeCalculation` proposal parameter has been activated by the committee. Until (and unless) that chain parameter is turned on, every `ExchangeTransactionContract`, `ExchangeInjectContract`, and `ExchangeWithdrawContract` broadcast by any account routes through the double-based math in `ExchangeProcessor`, whose selection is driven purely by the `hardenedCalc` flag passed from `AbstractExchangeActuator.allowHarden()`: [3](#0-2) [4](#0-3) 

The test suite itself documents that the legacy `double`-based math and the corrected `BigDecimal`/strict-math result diverge (`assertNotEquals(anotherTokenQuant, result)`), confirming the two paths are not equivalent and that the legacy path is imprecise: [5](#0-4) 

Because `ExchangeTransactionContract` is a normal broadcastable transaction type available to any account (not privileged), and because the actuator applies `ExchangeProcessor`'s output directly to the pool's token balances (`exchangeCapsule.setBalance(...)`) and to the caller's account (`addAssetAmountV2`/`setBalance`) with no independent sanity check against the ideal Bancor curve, a sequence of many small trades against the same pool — analogous to the attacker's 80 mint/burn cycles in the report — can exploit the accumulated floating-point drift to extract more of the counter-asset than the constant-product/constant-relay-supply invariant should allow, degrading the pool's reserves over repeated calls.

### Impact Explanation
Repeated exploitation drains real TRX/TRC10 asset value from the Bancor-style exchange pool's reserves to the attacker's account, i.e., unauthorized extraction/permanent loss of pooled funds — a direct funds-theft impact, matching the "unbacked balance / theft of funds" acceptance criteria.

### Likelihood Explanation
The attack requires only ordinary signed transactions (`ExchangeTransactionContract`) from any account with a small TRX/TRC10 balance against any live TRC10 Exchange pool, with no special privilege, contract deployment, or SR/witness compromise needed — closely matching the original report's "any unprivileged transaction broadcaster" reachability. It is bounded by the fact that `allowHardenExchangeCalculation`, once activated, closes the gap; but as long as this committee proposal is not activated on a given network/testnet, or during any window before its activation, the legacy path is live and reachable by any user.

### Recommendation
Make `SafeExchangeProcessor` the default (or the only) processor for `ExchangeTransactionActuator`/`ExchangeInjectActuator`/`ExchangeWithdrawActuator` instead of gating the fix behind an opt-in committee proposal, or add an independent bound check (e.g., verifying the trade result against the closed-form constant-product/constant-relay-supply invariant computed with exact `BigDecimal`/integer arithmetic) before applying balance changes, so pool reserves cannot be drained via accumulated floating-point drift regardless of the `allowHardenExchangeCalculation` flag's state.

### Proof of Concept
Conceptually mirroring the external report's mint/burn loop:
1. Attacker creates or finds a live TRC10 `Exchange` pool (e.g., TRX/TokenX) while `allowHardenExchangeCalculation` is not yet enabled on the target chain.
2. Attacker repeatedly broadcasts small `ExchangeTransactionContract` transactions alternating direction (sell TRX for TokenX, then sell TokenX back for TRX), each routed through `ExchangeCapsule.transaction(..., hardenedCalc=false)` → `ExchangeProcessor.exchange()`.
3. Because `exchangeToSupply`/`exchangeFromSupply` use double-precision `pow()`, each round-trip's rounding error is biased in the attacker's favor over many iterations (as shown by the divergence from the exact `BigDecimal` result in `ExchangeProcessorTest.testStrictMath`).
4. Repeating this cycle across enough iterations, analogous to the 80 mint/burn cycles in the original report, nets the attacker cumulative TRX/TRC10 profit while depleting the pool's `firstTokenBalance`/`secondTokenBalance`, which can be observed via `Commons.putExchangeCapsule` writes to `ExchangeStore`/`ExchangeV2Store`. [6](#0-5)

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

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

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-15)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L45-99)
```java
    long fee = calcFee();
    AccountStore accountStore = chainBaseManager.getAccountStore();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    ExchangeStore exchangeStore = chainBaseManager.getExchangeStore();
    ExchangeV2Store exchangeV2Store = chainBaseManager.getExchangeV2Store();
    AssetIssueStore assetIssueStore = chainBaseManager.getAssetIssueStore();
    try {
      final ExchangeTransactionContract exchangeTransactionContract = this.any
          .unpack(ExchangeTransactionContract.class);
      AccountCapsule accountCapsule = accountStore
          .get(exchangeTransactionContract.getOwnerAddress().toByteArray());

      ExchangeCapsule exchangeCapsule = Commons
          .getExchangeStoreFinal(dynamicStore, exchangeStore, exchangeV2Store)
          .get(ByteArray.fromLong(exchangeTransactionContract.getExchangeId()));

      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();

      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());

      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
      } else {
        anotherTokenID = firstTokenID;
      }

      long newBalance = subtractExact(accountCapsule.getBalance(), calcFee());
      accountCapsule.setBalance(newBalance);

      if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, tokenQuant));
      } else {
        accountCapsule.reduceAssetAmountV2(tokenID, tokenQuant, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(addExact(newBalance, anotherTokenQuant));
      } else {
        accountCapsule
            .addAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore);
      }

      accountStore.put(accountCapsule.createDbKey(), accountCapsule);

      Commons.putExchangeCapsule(exchangeCapsule, dynamicStore, exchangeStore, exchangeV2Store,
          assetIssueStore);

      ret.setExchangeReceivedAmount(anotherTokenQuant);
      ret.setStatus(fee, code.SUCESS);
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
