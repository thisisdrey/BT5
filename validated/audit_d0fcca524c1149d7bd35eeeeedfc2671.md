### Title
Unvalidated TRC10 Token Decimal Precision Leads to Incorrect Exchange/Market Price Calculations - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java], [File: actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java])

### Summary
TRC10 tokens carry an arbitrary `precision` field (0–6) set at issuance, but the AMM-style `Exchange` pools and the `Market` order-book actuators treat token quantities as raw, precision-agnostic integers when computing exchange ratios and swap outputs. Because there is no validation that paired tokens share comparable decimal precision, or any normalization of quantities by `precision` before performing price/ratio math, a pool or order created between tokens of differing precision produces a price that does not reflect real economic value, mirroring the reported "incorrect LP calculation from unvalidated decimal precision" bug class.

### Finding Description
`AssetIssueActuator.validate()` only bounds `precision` to `ActuatorConstant.PRECISION_DECIMAL` (6); it never enforces a canonical precision across tokens that might later be paired in an `Exchange` or `Market` order [1](#0-0) . `AssetIssueCapsule` stores this per-token `precision` purely as metadata used for the asset's own bookkeeping [2](#0-1) .

`ExchangeCreateActuator.doValidate()` accepts arbitrary `firstTokenID`/`secondTokenID` (TRC10 or TRX) with only balance-sufficiency and non-zero checks — it never inspects or compares the two tokens' `precision` values before initializing the pool balances used for future swaps [3](#0-2) . The actual swap math in `ExchangeProcessor`/`SafeExchangeProcessor` (bancor-style formulas) and `ExchangeCapsule.transaction()` operate purely on the raw `long` balances supplied at creation time, with no precision normalization [4](#0-3) [5](#0-4) .

Similarly, `MarketSellAssetActuator.validate()` looks up `AssetIssueCapsule` for `sellTokenID`/`buyTokenID` only to confirm existence, never reading or reconciling their `precision` fields against the raw `sellTokenQuantity`/`buyTokenQuantity` supplied by the caller [6](#0-5) . The resulting price ratio used for order matching (`MarketPrice.sellTokenQuantity`/`buyTokenQuantity`) is compared with plain integer cross-multiplication in `MarketComparator.comparePrice()`, again with no decimal adjustment [7](#0-6) .

A repo-wide search confirms `precision` is referenced only in asset-issuance validation/capsule/display code paths and is never read by `ExchangeCreateActuator`, `ExchangeWithdrawActuator`, `ExchangeInjectActuator`, `MarketSellAssetActuator`, `ExchangeProcessor`, `SafeExchangeProcessor`, or `MarketComparator` — i.e. it is dropped entirely once trading/pooling begins.

### Impact Explanation
Any account (an unprivileged transaction broadcaster) can issue two TRC10 tokens with different `precision` values (e.g., 0 vs. 6) via `AssetIssueActuator`, then create an `Exchange` pool or place `Market` orders pairing them via `ExchangeCreateContract`/`MarketSellAssetContract`. Because the pool/order math treats 1 raw unit of each token as equivalent for ratio purposes, the effective exchange rate is off by a factor of `10^|precisionA - precisionB|`. This allows a party controlling both a low-precision and high-precision token to seed a mispriced pool or order and drain the counter-asset from other participants who transact at face value, or to bootstrap outputs that grossly over- or under-value one side — a direct analog of the reported "incorrect LP calculations due to unvalidated oracle price decimal precision," resulting in unauthorized value transfer / theft of funds between traders.

### Likelihood Explanation
High. No special privilege is required — asset issuance, exchange/pool creation, and market order placement are all standard actuators reachable by any signed transaction from a regular account. The precision mismatch is trivially achievable since `AssetIssueActuator` allows any precision from 0–6 with no cross-token consistency requirement, and both `ExchangeCreateActuator` and `MarketSellAssetActuator` proceed to execute price math with no precision check whatsoever.

### Recommendation
Before allowing `ExchangeCreateActuator`/`ExchangeInjectActuator`/`ExchangeWithdrawActuator` to pair two tokens (or TRX with a token), fetch each token's `AssetIssueCapsule.getPrecision()` and either (a) reject pairs whose precision differs from a canonical value, or (b) normalize `firstTokenBalance`/`secondTokenBalance` (and all subsequent swap math) to a common decimal base before computing ratios. Apply the same normalization in `MarketSellAssetActuator.validate()`/`matchOrder()` and `MarketComparator.comparePrice()` so that `sellTokenQuantity`/`buyTokenQuantity` comparisons are precision-aware.

### Proof of Concept
1. Issue token A with `precision = 0` and token B with `precision = 6` via `AssetIssueContract` (both pass `AssetIssueActuator.validate()` since each is ≤ 6).
2. Call `ExchangeCreateContract` to create a pool with `firstTokenBalance = 1_000_000` of A and `secondTokenBalance = 1_000_000` of B — `ExchangeCreateActuator.doValidate()`/`execute()` accept this without any precision check.
3. Swap A for B (or vice versa) via `ExchangeInjectActuator`/withdraw, or place matching `MarketSellAssetContract` orders — the bancor-formula math in `ExchangeProcessor`/`SafeExchangeProcessor` and the raw ratio comparison in `MarketComparator.comparePrice()` treat 1 unit of A as worth 1 unit of B, even though B's real economic unit is 1,000,000× smaller (or larger) than A's, letting the attacker extract disproportionate value from the counter-party or pool.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java (L176-181)
```java
    int precision = assetIssueContract.getPrecision();
    if (precision != 0
        && dynamicStore.getAllowSameTokenName() != 0
        && (precision < 0 || precision > ActuatorConstant.PRECISION_DECIMAL)) {
      throw new ContractValidateException("precision cannot exceed 6");
    }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/AssetIssueCapsule.java (L87-95)
```java
  public int getPrecision() {
    return this.assetIssueContract.getPrecision();
  }

  public void setPrecision(int precision) {
    this.assetIssueContract = this.assetIssueContract.toBuilder()
        .setPrecision(precision)
        .build();
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L183-228)
```java
    byte[] firstTokenID = contract.getFirstTokenId().toByteArray();
    byte[] secondTokenID = contract.getSecondTokenId().toByteArray();
    long firstTokenBalance = contract.getFirstTokenBalance();
    long secondTokenBalance = contract.getSecondTokenBalance();

    if (dynamicStore.getAllowSameTokenName() == 1) {
      if (!Arrays.equals(firstTokenID, TRX_SYMBOL_BYTES) && !isNumber(firstTokenID)) {
        throw new ContractValidateException("first token id is not a valid number");
      }
      if (!Arrays.equals(secondTokenID, TRX_SYMBOL_BYTES) && !isNumber(secondTokenID)) {
        throw new ContractValidateException("second token id is not a valid number");
      }
    }

    if (Arrays.equals(firstTokenID, secondTokenID)) {
      throw new ContractValidateException("cannot exchange same tokens");
    }

    if (firstTokenBalance <= 0 || secondTokenBalance <= 0) {
      throw new ContractValidateException("token balance must greater than zero");
    }

    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    if (firstTokenBalance > balanceLimit || secondTokenBalance > balanceLimit) {
      throw new ContractValidateException("token balance must less than " + balanceLimit);
    }

    if (Arrays.equals(firstTokenID, TRX_SYMBOL_BYTES)) {
      if (accountCapsule.getBalance() < addExact(firstTokenBalance, calcFee())) {
        throw new ContractValidateException("balance is not enough");
      }
    } else {
      if (!accountCapsule.assetBalanceEnoughV2(firstTokenID, firstTokenBalance, dynamicStore)) {
        throw new ContractValidateException("first token balance is not enough");
      }
    }

    if (Arrays.equals(secondTokenID, TRX_SYMBOL_BYTES)) {
      if (accountCapsule.getBalance() < addExact(secondTokenBalance, calcFee())) {
        throw new ContractValidateException("balance is not enough");
      }
    } else {
      if (!accountCapsule.assetBalanceEnoughV2(secondTokenID, secondTokenBalance, dynamicStore)) {
        throw new ContractValidateException("second token balance is not enough");
      }
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

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-158)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L241-274)
```java
    try {
      // Whether the balance is enough
      long fee = calcFee();

      if (Arrays.equals(sellTokenID, "_".getBytes())) {
        if (ownerAccount.getBalance() < addExact(sellTokenQuantity, fee)) {
          throw new ContractValidateException("No enough balance !");
        }
      } else {
        if (ownerAccount.getBalance() < fee) {
          throw new ContractValidateException("No enough balance !");
        }

        AssetIssueCapsule assetIssueCapsule = Commons
            .getAssetIssueStoreFinal(dynamicStore, assetIssueStore, assetIssueV2Store)
            .get(sellTokenID);
        if (assetIssueCapsule == null) {
          throw new ContractValidateException("No sellTokenId !");
        }
        if (!ownerAccount.assetBalanceEnoughV2(sellTokenID, sellTokenQuantity,
            dynamicStore)) {
          throw new ContractValidateException("SellToken balance is not enough !");
        }
      }

      if (!Arrays.equals(buyTokenID, "_".getBytes())) {
        // Whether have the token
        AssetIssueCapsule assetIssueCapsule = Commons
            .getAssetIssueStoreFinal(dynamicStore, assetIssueStore, assetIssueV2Store)
            .get(buyTokenID);
        if (assetIssueCapsule == null) {
          throw new ContractValidateException("No buyTokenId !");
        }
      }
```

**File:** platform/src/main/java/common/org/tron/common/utils/MarketComparator.java (L69-86)
```java
  public static int comparePrice(long price1SellQuantity, long price1BuyQuantity,
                                 long price2SellQuantity, long price2BuyQuantity) {
    try {
      return Long.compare(StrictMath.multiplyExact(price1BuyQuantity, price2SellQuantity),
          StrictMath.multiplyExact(price2BuyQuantity, price1SellQuantity));

    } catch (ArithmeticException ex) {
      // do nothing here, because we will use BigInteger to compute again
    }

    BigInteger price1BuyQuantityBI = BigInteger.valueOf(price1BuyQuantity);
    BigInteger price1SellQuantityBI = BigInteger.valueOf(price1SellQuantity);
    BigInteger price2BuyQuantityBI = BigInteger.valueOf(price2BuyQuantity);
    BigInteger price2SellQuantityBI = BigInteger.valueOf(price2SellQuantity);

    return price1BuyQuantityBI.multiply(price2SellQuantityBI)
        .compareTo(price2BuyQuantityBI.multiply(price1SellQuantityBI));
  }
```
