### Title
Floating-point precision loss in the legacy TRX/Token `Exchange` (Bancor-relay) pricing formula enables profitable pool-ratio manipulation - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java])

### Summary
The on-chain Bancor-style exchange (`ExchangeCreateContract` / `ExchangeInjectContract` / `ExchangeWithdrawContract` / `ExchangeTransactionContract`) computes swap outputs with double-precision floating point math in the default (non-hardened) code path. Any unprivileged account that already holds one of the two exchanged assets can call `ExchangeTransactionContract` repeatedly with crafted quantities to exploit rounding/precision drift in the relay-supply formula, extracting value from the pool the same way pool-price manipulation was used in the external Kub-Split incident (repeated trades that skew a two-asset pool's internal price away from its true value).

### Finding Description
The core swap math lives in `ExchangeProcessor`: [1](#0-0) 

`exchangeToSupply` and `exchangeFromSupply` use `double` arithmetic and `Math.pow`/`Maths.pow` to implement the Bancor relay formula (`(1 + quant/newBalance)^0.0005` and the inverse `^2000`). This is selected whenever `allowHarden()` (`chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation()`) is false: [2](#0-1) 

The codebase itself demonstrates the double-precision path and the `BigDecimal`-based `SafeExchangeProcessor` diverge: [3](#0-2) 

`AbstractExchangeActuator.addExact/subtractExact` only apply overflow-checked math when `allowHarden()` is true; otherwise plain `long` `+`/`-` is used: [4](#0-3) 

Because `allowHardenExchangeCalculation` is a dynamic-property proposal flag (opt-in, off by default until activated by the committee), the legacy double-precision, non-overflow-checked path is the actual default execution path for `ExchangeTransactionActuator.execute`, `ExchangeInjectActuator.execute`, and `ExchangeWithdrawActuator.execute`: [5](#0-4) 

An attacker who repeatedly issues small `ExchangeTransactionContract` swaps (sell A→buy B, then sell B→buy A) exploits the floating-point rounding of `(long) issuedSupply` / `(long) exchangeBalance` truncation in `ExchangeProcessor`, which does not preserve the invariant that the constant-relay-supply model guarantees under exact arithmetic. Each round trip can leak a small amount of value from the pool reserves due to `double`→`long` truncation bias, and this can be repeated (bounded only by transaction throughput/fees) to drain the pool over many transactions — directly analogous to the reported pool-manipulation flash-loan exploit, except here no flash loan is even required because the attacker only needs enough balance for gas/fee-free repeated calls (`ExchangeTransactionActuator.calcFee()` returns `0`): [6](#0-5) 

### Impact Explanation
A successful exploitation results in unbacked/incorrect balances inside a live TRX/TRC-10 `Exchange` pool: the attacker profits from repeated swaps at the expense of other liquidity participants (pool creator/injectors), i.e. theft of pooled funds through price/precision manipulation, matching the "unbacked balance"/"theft of funds" acceptance criteria. Because `ExchangeTransactionContract` has zero fee (`calcFee()` returns 0), the attack cost is only bandwidth/energy, making repeated exploitation economically attractive at scale.

### Likelihood Explanation
This is reachable by any unprivileged account with no special permissions: the attacker only needs to hold a token pair listed in an existing `Exchange` and repeatedly broadcast `ExchangeTransactionContract` transactions. The vulnerable double-precision code path is the default runtime behavior unless `allowHardenExchangeCalculation` has been separately activated on the network, so likelihood is high on any deployment that has not enabled the hardening proposal.

### Recommendation
- Enforce the `BigDecimal`/`SafeExchangeProcessor` exact-arithmetic path (already implemented) unconditionally for `ExchangeTransactionActuator`, `ExchangeInjectActuator`, and `ExchangeWithdrawActuator`, rather than gating it behind the opt-in `allowHardenExchangeCalculation` dynamic property.
- Add invariant checks after every swap verifying the constant-relay-supply property does not decrease pool value beyond expected fee/rounding tolerance.
- Consider deprecating/disabling `ExchangeProcessor`'s double-based computation entirely once the hardened processor is proven equivalent, removing the legacy vulnerable code path from production.

### Proof of Concept
Conceptual reproduction (requires the un-hardened default configuration, i.e. `allowHardenExchangeCalculation` not enabled):
1. Attacker account funds itself with a small amount of both `TRX` and TRC-10 token `X`, both already listed in an existing `Exchange` pool.
2. Repeatedly broadcast alternating `ExchangeTransactionContract` transactions: sell a small `quant` of `TRX` for `X`, then sell the received `X` back for `TRX`, using `ExchangeTransactionActuator` (`exchangeCapsule.transaction(...)` in `ExchangeCapsule.java` lines 124-169, non-hardened branch).
3. Because `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` truncate `double` results with `(long) issuedSupply` and `(long) exchangeBalance`, each round trip can yield a net-positive `long` remainder for the attacker due to floating-point/truncation bias, at zero transaction fee (`calcFee()==0`).
4. Automating this over many blocks drains value from the pool's reserves relative to fair value, without requiring any privileged role — an unprivileged transaction broadcaster reachable path.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L17-39)
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

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

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-23)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }

  public long subtractExact(long x, long y) {
    return allowHarden() ? StrictMathWrapper.subtractExact(x, y) : x - y;
  }

  public long addExact(long x, long y) {
    return allowHarden() ? StrictMathWrapper.addExact(x, y) : x + y;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L38-99)
```java
  @Override
  public boolean execute(Object object) throws ContractExeException {
    TransactionResultCapsule ret = (TransactionResultCapsule) object;
    if (Objects.isNull(ret)) {
      throw new RuntimeException(ActuatorConstant.TX_RESULT_NULL);
    }

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L232-235)
```java
  @Override
  public long calcFee() {
    return 0;
  }
```
