### Title
TRC10 Exchange/Market actuators ignore per-token `precision`, letting differing-decimal token pairs be swapped at a distorted rate - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`, `chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java`, `actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java`, `chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java`)

### Summary
TRC10 assets can be issued with a configurable `precision` (0–6 decimals) via `AssetIssueContract.precision`, validated in `AssetIssueActuator.validate()`. [1](#0-0) 
Both the on-chain Bancor-style exchange (`ExchangeCreateActuator`/`ExchangeInjectActuator`/`ExchangeWithdrawActuator`/`ExchangeCapsule`) and the order-book market (`MarketSellAssetActuator`/`MarketUtils`) never reference `precision` anywhere in their swap/ratio arithmetic — a grep for "precision" across all `*Exchange*.java` and `*Market*.java` production files returns zero hits outside of test files. This is the same root cause as the reported DAI-to-sell decimal scaling bug: raw token-balance/quantity ratios are used directly as exchange rates without normalizing for each token's decimal precision.

### Finding Description
`ExchangeInjectActuator.doValidate()` computes the paired-token quantity purely from raw on-chain balances:
```
anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant).divide(bigFirstTokenBalance)...
``` [2](#0-1) 
`ExchangeWithdrawActuator.doValidate()` performs the same raw-balance ratio calculation. [3](#0-2) 
The core swap math in `ExchangeCapsule.transaction()` / `ExchangeProcessor.exchange()` operates purely on the raw `firstTokenBalance`/`secondTokenBalance` longs stored in the pool, again with no decimal normalization. [4](#0-3) [5](#0-4) 

The order-book market has the identical issue: `MarketSellAssetActuator.matchSingleOrder()` computes fill quantities using `MarketUtils.multiplyAndDivide(takerSellRemainQuantity, makerSellQuantity, makerBuyQuantity, ...)`, a pure integer ratio with no decimal adjustment. [6](#0-5) 
`MarketUtils.multiplyAndDivide` and the price-key/GCD logic in `MarketUtils.createPairPriceKey` similarly work on raw `sellTokenQuantity`/`buyTokenQuantity` longs. [7](#0-6) [8](#0-7) 

Because `AssetIssueActuator` allows independently-chosen `precision` per TRC10 asset (0 through 6), [9](#0-8) 
a pair of TRC10 tokens with different `precision` (analogous to WBTC's 8 decimals vs DAI's 18 decimals in the referenced report) will have their raw integer balances/quantities interpreted as a 1:1 nominal exchange rate by both the Bancor exchange and the order-book market, even though 1 raw unit of a precision-0 token represents 10^N times more "real" value than 1 raw unit of a precision-N token.

### Impact Explanation
An attacker can create an exchange pool (`ExchangeCreateActuator`) or place market orders (`MarketSellAssetActuator`) pairing a low-precision TRC10 asset against a high-precision one, then inject/withdraw or fill orders to obtain the higher-precision (larger nominal unit-value) token far below its true value, or drain a counterparty's/pool's holdings of the mispriced side. This is unauthorized value extraction / theft of funds from other exchange participants and pool liquidity providers — a concrete "theft of funds" impact reachable purely by broadcasting standard signed transactions (`AssetIssueContract`, `ExchangeCreateContract`, `ExchangeInjectContract`, `ExchangeWithdrawContract`, `MarketSellAssetContract`).

### Likelihood Explanation
Any account can issue a TRC10 asset with an arbitrary `precision` (0–6) and any account can create an exchange pool or place market orders pairing arbitrary TRC10 tokens — no privileged role is required. The mis-scaled math is unconditionally triggered whenever two tokens of differing precision are paired, making this trivially and repeatably reachable by any unprivileged transaction broadcaster.

### Recommendation
Store and apply a decimal-normalization factor (derived from each token's `precision`) whenever computing exchange ratios, pool balances, or market order fill quantities in `ExchangeCapsule`, `ExchangeProcessor`, `ExchangeInjectActuator`, `ExchangeWithdrawActuator`, `MarketSellAssetActuator`, and `MarketUtils`, so that quantities are compared/converted in a common decimal base rather than as raw integer units.

### Proof of Concept
1. Issue TRC10 token `A` with `precision = 0` and TRC10 token `B` with `precision = 6` via `AssetIssueActuator` (validated range 0–6 per `AssetIssueActuator.validate()`).
2. Create a Bancor exchange pool (or place a market sell order via `MarketSellAssetActuator`) pairing `A` and `B` with equal raw quantities, e.g., `firstTokenBalance = 1_000_000` of `A` and `secondTokenBalance = 1_000_000` of `B`.
3. Because `ExchangeCapsule.transaction()` / `MarketUtils.multiplyAndDivide()` only compare raw longs, the pool/order book treats `1_000_000` raw units of `A` (1,000,000 whole tokens at precision 0) as equal in value to `1_000_000` raw units of `B` (1 whole token at precision 6) — a 10^6 mispricing.
4. The attacker sells/injects the cheap, low-precision token and withdraws/receives the expensive, high-precision token at the distorted 1:1 raw-unit rate, extracting real value from the counterparty/pool.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L215-227)
```java
    if (Arrays.equals(tokenID, firstTokenID)) {
      anotherTokenID = secondTokenID;
      anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
          .divide(bigFirstTokenBalance).longValueExact();
      newTokenBalance = addExact(firstTokenBalance, tokenQuant);
      newAnotherTokenBalance = addExact(secondTokenBalance, anotherTokenQuant);
    } else {
      anotherTokenID = firstTokenID;
      anotherTokenQuant = bigFirstTokenBalance.multiply(bigTokenQuant)
          .divide(bigSecondTokenBalance).longValueExact();
      newTokenBalance = addExact(secondTokenBalance, tokenQuant);
      newAnotherTokenBalance = addExact(firstTokenBalance, anotherTokenQuant);
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L214-247)
```java
    BigDecimal bigFirstTokenBalance = new BigDecimal(String.valueOf(firstTokenBalance));
    BigDecimal bigSecondTokenBalance = new BigDecimal(String.valueOf(secondTokenBalance));
    BigDecimal bigTokenQuant = new BigDecimal(String.valueOf(tokenQuant));
    final boolean allowHarden = allowHarden();
    if (Arrays.equals(tokenID, firstTokenID)) {
      anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
          .divideToIntegralValue(bigFirstTokenBalance).longValueExact();
      if (firstTokenBalance < tokenQuant || secondTokenBalance < anotherTokenQuant) {
        throw new ContractValidateException("exchange balance is not enough");
      }

      if (anotherTokenQuant <= 0) {
        throw new ContractValidateException("withdraw another token quant must greater than zero");
      }
      if (allowHarden) {
        BigDecimal remainder = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance, 4, RoundingMode.HALF_UP)
            .subtract(BigDecimal.valueOf(anotherTokenQuant));
        if (remainder.compareTo(
            BigDecimal.valueOf(anotherTokenQuant).multiply(new BigDecimal("0.0001"))) > 0) {
          throw new ContractValidateException("Not precise enough");
        }
      } else {
        double remainder = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance, 4, BigDecimal.ROUND_HALF_UP).doubleValue()
            - anotherTokenQuant;
        if (remainder / anotherTokenQuant > 0.0001) {
          throw new ContractValidateException("Not precise enough");
        }
      }

    } else {
      anotherTokenQuant = bigFirstTokenBalance.multiply(bigTokenQuant)
          .divideToIntegralValue(bigSecondTokenBalance).longValueExact();
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

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L41-45)
```java
  @Override
  public long exchange(long sellTokenBalance, long buyTokenBalance, long sellTokenQuant) {
    long relay = exchangeToSupply(sellTokenBalance, sellTokenQuant);
    return exchangeFromSupply(buyTokenBalance, relay);
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L393-404)
```java
    // according to the price of maker, calculate the quantity of taker can buy
    // for makerPrice,sellToken is A,buyToken is TRX.
    // for takerPrice,buyToken is A,sellToken is TRX.

    // makerSellTokenQuantity_A/makerBuyTokenQuantity_TRX =
    //   takerBuyTokenQuantityCurrent_A/takerSellTokenQuantityRemain_TRX
    // => takerBuyTokenQuantityCurrent_A = takerSellTokenQuantityRemain_TRX *
    //   makerSellTokenQuantity_A/makerBuyTokenQuantity_TRX

    long takerBuyTokenQuantityRemain = MarketUtils
        .multiplyAndDivide(takerSellRemainQuantity, makerSellQuantity, makerBuyQuantity,
            this.disableJavaLangMath());
```

**File:** chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java (L85-103)
```java
  public static byte[] createPairPriceKey(byte[] sellTokenId, byte[] buyTokenId,
      long sellTokenQuantity, long buyTokenQuantity) {

    byte[] sellTokenQuantityBytes;
    byte[] buyTokenQuantityBytes;

    // cal the GCD
    long gcd = findGCD(sellTokenQuantity, buyTokenQuantity);
    if (gcd == 0) {
      sellTokenQuantityBytes = ByteArray.fromLong(sellTokenQuantity);
      buyTokenQuantityBytes = ByteArray.fromLong(buyTokenQuantity);
    } else {
      sellTokenQuantityBytes = ByteArray.fromLong(sellTokenQuantity / gcd);
      buyTokenQuantityBytes = ByteArray.fromLong(buyTokenQuantity / gcd);
    }

    return doCreatePairPriceKey(sellTokenId, buyTokenId,
        sellTokenQuantityBytes, buyTokenQuantityBytes);
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java (L264-277)
```java
  public static long multiplyAndDivide(long a, long b, long c, boolean disableMath) {
    try {
      long tmp = multiplyExact(a, b, disableMath);
      return floorDiv(tmp, c, disableMath);
    } catch (ArithmeticException ex) {
      // do nothing here, because we will use BigInteger to compute again
    }

    BigInteger aBig = BigInteger.valueOf(a);
    BigInteger bBig = BigInteger.valueOf(b);
    BigInteger cBig = BigInteger.valueOf(c);

    return aBig.multiply(bBig).divide(cBig).longValue();
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/AssetIssueCapsule.java (L87-89)
```java
  public int getPrecision() {
    return this.assetIssueContract.getPrecision();
  }
```
