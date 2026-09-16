### Title
Non-hardened Bancor-style Exchange swap uses floating-point pow() math, allowing precision manipulation of AMM balances - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java])

### Summary
`ExchangeTransactionActuator` lets any TRX/TRC10 holder swap against a Bancor-formula liquidity pool (`ExchangeCapsule`). Unless `allowHardenExchangeCalculation` is enabled, the pool math is delegated to `ExchangeProcessor`, which computes swap output using `double`-based `Math.pow` calls instead of exact integer/BigDecimal math. [1](#0-0) [2](#0-1) 

### Finding Description
`ExchangeCapsule.transaction()` selects between two `Processor` implementations depending on `hardenedCalc` (driven by `dynamicStore`'s `allowHardenExchangeCalculation` flag): the exact `SafeExchangeProcessor` (BigDecimal, `RoundingMode.HALF_UP`, guarded against negative post-balances) or the legacy `ExchangeProcessor`, which performs the Bancor "supply -> balance" conversion entirely with primitive `double` arithmetic and `Math.pow`. [3](#0-2) [4](#0-3) 

Unlike `SafeExchangeProcessor`, which explicitly rejects any resulting negative token balance (`if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) throw ...`), the legacy path performs no such post-condition check anywhere in `ExchangeCapsule.transaction()` when `hardenedCalc` is false: the new balances are simply written back after the double-precision calculation. [5](#0-4) 

Because floating-point `pow()`/division introduces rounding error that is not controlled or bounded (no `RoundingMode`, no minimum precision guarantee, and results are truncated via a `(long)` cast that always rounds toward zero rather than being validated against an exact integer ratio), a caller can choose `sellTokenQuant` values at pool-balance extremes (e.g., very small pool balances, or `sellTokenQuant` close to `newBalance`) to bias the double-precision error systematically in their favor across repeated calls. This mirrors the ValueDeFi incident's root cause class: an AMM/vault "price" calculation that is not computed with implementation-exact, monotonic-safe arithmetic, letting an attacker repeatedly extract value through calculation drift rather than through a single large trade that would trip balance/expected-output checks.

This is only reachable for exchanges created and left in the pre-"harden" mode; once `allowHardenExchangeCalculation` is enabled, all swaps route to `SafeExchangeProcessor.INSTANCE`, which does not carry this weakness. The severity depends on how many live Exchange pools on mainnet still operate with the flag disabled, which cannot be determined from static code alone.

### Impact Explanation
If floating-point drift can be reliably steered (e.g., through the choice of `sellTokenQuant`, or by chaining many small swaps to accumulate one-directional rounding bias), an attacker can extract more value from the pool than the exact Bancor formula would allow, draining the counter-asset balance held by the Exchange's creator (who funded the pool) — a direct "theft of funds" outcome consistent with the report's DeFi-AMM-drain bug class. Because `ExchangeTransactionActuator` is callable by any funded account via a single signed transaction and has no additional caller restriction (unlike `ExchangeInjectActuator`/`ExchangeWithdrawActuator`, which require the caller to be the exchange creator), this is reachable by an unprivileged transaction broadcaster. [6](#0-5) 

### Likelihood Explanation
Exploitability depends on (a) whether any exchange with meaningful liquidity still has `allowHardenExchangeCalculation` disabled, and (b) whether the accumulated double-precision bias is large enough per trade to be profitable net of the `MarketSellFee`/gas-equivalent bandwidth/energy costs of repeated transactions. I could not verify from the available index whether `allowHardenExchangeCalculation` is now permanently enabled on mainnet (i.e., whether this is a legacy/dead code path), nor could I quantify the magnitude of the floating-point drift without running the formula against real pool sizes — this uncertainty should be resolved with dynamic testing/fuzzing of `ExchangeProcessor.exchange()` against `SafeExchangeProcessor.exchange()` for divergence, and by checking on-chain whether the harden flag is set for all active TRC10 exchange pairs.

### Recommendation
- Confirm whether `allowHardenExchangeCalculation` is unconditionally enabled network-wide (a maintenance/hard-fork proposal), and if so, ensure `ExchangeProcessor` can no longer be selected regardless of stored per-account/legacy state.
- If the legacy path must remain reachable, port the same negative-balance / precision-drift guard used in `SafeExchangeProcessor` (BigDecimal-based, `HALF_UP` rounding, explicit balance validation) into `ExchangeProcessor`, or remove `ExchangeProcessor` and always use `SafeExchangeProcessor.INSTANCE`.
- Add a fuzz/property test comparing `ExchangeProcessor.exchange()` output against a BigDecimal reference implementation across many pool-size/quant combinations to bound the maximum extractable drift per trade, and enforce that bound during validation.

### Proof of Concept
Concrete exploitation requires numerically fuzzing `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` against pool balances to find quant values that produce favorable double-precision truncation; this could not be executed in this static-analysis-only environment. The code path enabling the issue is fully reachable via a single `ExchangeTransactionContract` transaction when the target exchange has `allowHardenExchangeCalculation` disabled: [7](#0-6) [2](#0-1)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-69)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();

      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L119-170)
```java
  private boolean doValidate() throws ContractValidateException {
    if (this.any == null) {
      throw new ContractValidateException(ActuatorConstant.CONTRACT_NOT_EXIST);
    }
    if (chainBaseManager == null) {
      throw new ContractValidateException(ActuatorConstant.STORE_NOT_EXIST);
    }
    AccountStore accountStore = chainBaseManager.getAccountStore();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    ExchangeStore exchangeStore = chainBaseManager.getExchangeStore();
    ExchangeV2Store exchangeV2Store = chainBaseManager.getExchangeV2Store();
    if (!this.any.is(ExchangeTransactionContract.class)) {
      throw new ContractValidateException(
          "contract type error,expected type [ExchangeTransactionContract],real type[" + any
              .getClass() + "]");
    }
    final ExchangeTransactionContract contract;
    try {
      contract = this.any.unpack(ExchangeTransactionContract.class);
    } catch (InvalidProtocolBufferException e) {
      throw new ContractValidateException(e.getMessage());
    }

    byte[] ownerAddress = contract.getOwnerAddress().toByteArray();
    String readableOwnerAddress = StringUtil.createReadableString(ownerAddress);

    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid address");
    }

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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-168)
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

    } else {
      buyTokenQuant = processor.exchange(secondTokenBalance,
          firstTokenBalance,
          sellTokenQuant);
      newFirstTokenBalance = hardenedCalc
          ? StrictMathWrapper.subtractExact(firstTokenBalance, buyTokenQuant)
          : firstTokenBalance - buyTokenQuant;
      newSecondTokenBalance = hardenedCalc
          ? StrictMathWrapper.addExact(secondTokenBalance, sellTokenQuant)
          : secondTokenBalance + sellTokenQuant;

    }

    if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
      throw new ContractValidateException("Exchange balance must be >=0 after transaction");
    }
    this.exchange = this.exchange.toBuilder()
        .setFirstTokenBalance(newFirstTokenBalance)
        .setSecondTokenBalance(newSecondTokenBalance)
        .build();

    return buyTokenQuant;
```

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
