### Title
Floating-point precision loss in TRC10 Bancor-relay Exchange formula enables profit-inflation via repeated small trades - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java`)

### Summary
The `pSPACE` incident is a classic bonding-curve/AMM "profit-inflation" bug where an attacker exploits rounding/precision asymmetry in a supply-based pricing formula to extract more value than deposited. The analogous surface in java-tron is the TRC10 Bancor-relay exchange (`ExchangeCreateContract`/`ExchangeTransactionContract`), whose default (non-hardened) pricing math is computed entirely with Java `double` floating point and truncated to `long`, rather than using exact fixed-point/`BigDecimal` arithmetic.

### Finding Description
`ExchangeCapsule.transaction()` selects a pricing engine based on the `allowHardenExchangeCalculation` dynamic parameter [1](#0-0) . When this hardening flag is disabled (its default historically), the legacy `ExchangeProcessor` is used, which computes the virtual-supply bonding-curve exchange purely in `double` precision: [2](#0-1) 

Both `exchangeToSupply` and `exchangeFromSupply` use `Math.pow`-based floating point computation and truncate the result by casting `double` to `long` (`(long) issuedSupply`, `(long) exchangeBalance`). This is analogous to the pSPACE bonding-curve/first-depositor style bug class: repeated small-quantity trades against the pool can accumulate systematic rounding bias because the truncation direction and magnitude depend nonlinearly on `quant`/`balance` ratios computed via floating point, unlike the hardened `SafeExchangeProcessor` path which explicitly performs the same math with `BigDecimal` at 18-digit scale and `RoundingMode.HALF_UP` [3](#0-2) .

Any unprivileged account can reach this code by broadcasting `ExchangeTransactionContract` transactions — `ExchangeTransactionActuator.validate()`/`execute()` do not require the caller to be the exchange creator (unlike `ExchangeInjectActuator`/`ExchangeWithdrawActuator`, which explicitly check `accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())`) [4](#0-3) . The only economic check is `anotherTokenQuant < tokenExpected` [5](#0-4) , which an attacker fully controls by setting `expected` to whatever the (biased) formula returns.

### Impact Explanation
If an attacker identifies input ranges where the floating-point truncation in `ExchangeProcessor` systematically favors the trader over many iterations (e.g., trading dust amounts against a low-liquidity, attacker-created exchange pool), they can repeatedly drain the counter-token reserve of an `Exchange` pool beyond what the bonding-curve math should allow, extracting real TRX/TRC10 value that was never economically backed — a direct analog of the pSPACE "profit-inflation" flash-loan exploit. Because pools can be attacker-created (`ExchangeCreateContract` is open to any account with sufficient balance) and trading is open to any account, this is reachable end-to-end from a single signed transaction stream without any privileged role, and results in theft of TRC10/TRX funds seeded into that exchange.

### Likelihood Explanation
The `ExchangeProcessor` (unhardened, floating-point) path is the default execution path unless `allowHardenExchangeCalculation` has been activated network-wide, and `ExchangeTransactionContract` requires no special permission — any address holding the sell-side token/TRX can submit repeated trades against any Exchange pool. Exploitation only requires crafting a self-created low-liquidity pool and looping small trades, which is inexpensive (bounded by TRX bandwidth/energy fees), making this readily reachable if the vulnerable floating-point path is still active on a given deployment.

### Recommendation
- Ensure `allowHardenExchangeCalculation` (routing to `SafeExchangeProcessor`'s `BigDecimal` arithmetic) is permanently enabled and remove/deprecate the legacy `double`-based `ExchangeProcessor` code path entirely rather than gating it behind a toggle.
- Add invariant checks after every `exchange()` call verifying that the constant-product/bonding-curve invariant (`firstTokenBalance * secondTokenBalance`, adjusted for fees) does not decrease beyond the expected rounding tolerance, rejecting the transaction otherwise.
- Add a minimum trade-size / anti-dust threshold to `ExchangeTransactionActuator.doValidate()` to prevent grinding attacks via many tiny trades.

### Proof of Concept
1. Attacker calls `ExchangeCreateContract` to create a small, self-owned Exchange pool (e.g., 100 TRX vs 100 units of an attacker-issued TRC10 token) — no privileged role required.
2. With `allowHardenExchangeCalculation` disabled (default legacy path), attacker repeatedly submits `ExchangeTransactionContract` transactions with small `quant` values selected to bias the `(long) issuedSupply` / `(long) exchangeBalance` truncation in `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` in the attacker's favor.
3. Each iteration nets the attacker slightly more counter-token than the true bonding-curve price implies; repeated over many blocks this drains the pool's counter-token reserve, analogous to the pSPACE profit-inflation flash-loan attack, without ever violating the single `tokenExpected` check in `ExchangeTransactionActuator.doValidate()`.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

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

**File:** chainbase/src/main/java/org/tron/core/capsule/SafeExchangeProcessor.java (L19-44)
```java
  private BigDecimal exchangeToSupply(long balance, long quant) {
    long newBalance = StrictMathWrapper.addExact(balance, quant);
    BigDecimal bdQuant = BigDecimal.valueOf(quant);
    BigDecimal bdNewBalance = BigDecimal.valueOf(newBalance);
    BigDecimal base = BigDecimal.ONE.add(
        bdQuant.divide(bdNewBalance, 18, RoundingMode.HALF_UP));
    double powResult = StrictMathWrapper.pow(base.doubleValue(), 0.0005);
    return SUPPLY.negate().multiply(
        BigDecimal.ONE.subtract(BigDecimal.valueOf(powResult))).setScale(0, RoundingMode.DOWN);
  }

  private long exchangeFromSupply(long balance, BigDecimal supplyQuant) {
    BigDecimal bdBalance = BigDecimal.valueOf(balance);
    BigDecimal base = BigDecimal.ONE.add(
        supplyQuant.divide(SUPPLY, 18, RoundingMode.HALF_UP));
    double powResult = StrictMathWrapper.pow(base.doubleValue(), 2000.0);
    BigDecimal exchangeBalance = bdBalance.multiply(
        BigDecimal.valueOf(powResult).subtract(BigDecimal.ONE));
    return exchangeBalance.setScale(0, RoundingMode.DOWN).longValueExact();
  }

  @Override
  public long exchange(long sellTokenBalance, long buyTokenBalance, long sellTokenQuant) {
    BigDecimal relay = exchangeToSupply(sellTokenBalance, sellTokenQuant);
    return exchangeFromSupply(buyTokenBalance, relay);
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L119-224)
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
      throw new ContractValidateException("token is not in exchange");
    }

    if (tokenQuant <= 0) {
      throw new ContractValidateException("token quant must greater than zero");
    }

    if (tokenExpected <= 0) {
      throw new ContractValidateException("token expected must greater than zero");
    }

    if (firstTokenBalance == 0 || secondTokenBalance == 0) {
      throw new ContractValidateException("Token balance in exchange is equal with 0,"
          + "the exchange has been closed");
    }

    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    long tokenBalance = (Arrays.equals(tokenID, firstTokenID) ? firstTokenBalance
        : secondTokenBalance);
    tokenBalance = addExact(tokenBalance, tokenQuant);
    if (tokenBalance > balanceLimit) {
      throw new ContractValidateException("token balance must less than " + balanceLimit);
    }

    if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
      if (accountCapsule.getBalance() < addExact(tokenQuant, calcFee())) {
        throw new ContractValidateException("balance is not enough");
      }
    } else {
      if (!accountCapsule.assetBalanceEnoughV2(tokenID, tokenQuant, dynamicStore)) {
        throw new ContractValidateException("token balance is not enough");
      }
    }

    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }

    return true;
  }
```
