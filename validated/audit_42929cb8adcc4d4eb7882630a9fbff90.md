### Title
Unsafe BigInteger-to-`long` truncation in TRC10 market order matching math allows unbacked token/TRX credit — (File: `chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java`)

### Summary
`MarketUtils.multiplyAndDivide` computes `a * b / c` for TRC10 market order matching. When the fast `long` path (`multiplyExact`/`floorDiv`) overflows, it falls back to `BigInteger` arithmetic but converts the final quotient back to `long` with the *non-throwing* `BigInteger.longValue()`, which silently truncates/wraps on overflow instead of reverting — the same unsafe-cast class described in the external report (an unchecked cast of an accumulated/derived value that can exceed the target integer width). [1](#0-0) 

### Finding Description
`matchSingleOrder` in `MarketSellAssetActuator` (reachable from any signed `MarketSellAssetContract` transaction that matches against an existing order book entry) computes the amount of token/TRX to credit both taker and maker using this helper: [2](#0-1) [3](#0-2) [4](#0-3) 

The result of `multiplyAndDivide` is fed directly into balance/asset credit logic: [5](#0-4) 

Note the contrast with the codebase's own "hardened" pattern used elsewhere for the same class of computation (`ExchangeInjectActuator`, `SafeExchangeProcessor`), which explicitly use `longValueExact()` to *throw* rather than silently wrap on overflow: [6](#0-5) [7](#0-6) 

`MarketUtils.multiplyAndDivide`, by contrast, does not validate that the final `BigInteger` quotient fits in a `long` before calling `.longValue()`: [1](#0-0) 

Order quantities are individually bounded by the governance-configured `dynamicStore.getMarketQuantityLimit()` in `MarketSellAssetActuator.validate()`, but that bound is applied per-field, not to the product/quotient `a*b/c`: [8](#0-7) 

Because `c` (the maker's opposing-side quantity) can be as small as `1` while `a` and `b` can each independently reach `quantityLimit`, the product `a*b` can be forced far above `Long.MAX_VALUE`, and the division by `c=1` does not bring the value back into a safe range. In that case `.longValue()` truncates the high-order bits of an arbitrary-precision value into an unrelated (and potentially unpredictable/large or even negative-looking, but reinterpreted as unsigned magnitude) `long`, which is then used to credit TRX or TRC10 token balances via `addTrxOrToken`/`addExact`.

### Impact Explanation
If exploitable, this allows a user to construct a maker order with an extreme price ratio (tiny buy quantity, large sell quantity) and a matching taker order, causing the "amount received" computed by `multiplyAndDivide` to be an attacker-influenced garbage value rather than the economically correct exchange amount. Because the value is inserted directly into `AccountCapsule` balance/asset fields, this can result in unbacked TRX or TRC10 token credit (funds materializing without being backed by any counterparty's deposit) — a form of unauthorized value creation, analogous in class (unsafe cast on an accumulated arithmetic result) to the reported Merit Circle issue, but the java-tron truncation failure mode is silent corruption rather than a revert, making it potentially more severe (fund creation) rather than merely a stuck-fund DoS.

### Likelihood Explanation
Exploitability depends critically on the concrete value of the committee-configured `MarketQuantityLimit` dynamic parameter relative to `Long.MAX_VALUE` (≈9.22×10¹⁸). I was not able to confirm the deployed/default value of `getMarketQuantityLimit()` from the available index, so I cannot definitively confirm whether legitimately-created orders (each individually ≤ `quantityLimit`) can produce a product `a*b` large enough to overflow after division by a small `c`. If `quantityLimit` is set conservatively (e.g., well below the square root of `Long.MAX_VALUE`), the path may be unreachable in practice; if it is set to a large value (e.g., matching TRC10's maximum representable supply), the overflow is directly reachable via two ordinary signed `MarketSellAssetContract` transactions.

### Recommendation
In `MarketUtils.multiplyAndDivide`, after computing the `BigInteger` quotient, validate that it fits within `long` bounds using `longValueExact()` (throwing on overflow) instead of the silently-truncating `longValue()`, consistent with the hardened pattern already used in `ExchangeInjectActuator`/`SafeExchangeProcessor`. Additionally, consider bounding `MarketQuantityLimit` (or explicitly bounding the price ratio `sellTokenQuantity`/`buyTokenQuantity`) so that `a*b` can never exceed a safely representable range regardless of `c`.

### Proof of Concept
Conceptual (bounds on `MarketQuantityLimit` not fully confirmed from the index):
1. Attacker A places a maker sell order via `MarketSellAssetContract` with `sellTokenQuantity = quantityLimit` (large) and `buyTokenQuantity = 1` (minimum allowed by `validate()`), for token pair `TRX <-> TOKEN_X`.
2. Attacker B (can be the same or a colluding account) places a matching taker `MarketSellAssetContract` order with `sellTokenQuantity` also near `quantityLimit` for the opposite side.
3. In `MarketSellAssetActuator.matchSingleOrder`, `MarketUtils.multiplyAndDivide(takerSellRemainQuantity, makerSellQuantity, makerBuyQuantity=1, ...)` computes `a*b/1`; if `a*b` exceeds `Long.MAX_VALUE`, the `BigInteger` fallback's `.longValue()` truncates to an attacker-uncontrolled but non-reverting `long`.
4. That truncated value is credited to the taker's account balance/asset map via `addTrxOrToken`, potentially crediting more value than was ever deposited by the counterparty, i.e., minting unbacked balance. [1](#0-0) [8](#0-7)

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L223-230)
```java
    if (sellTokenQuantity <= 0 || buyTokenQuantity <= 0) {
      throw new ContractValidateException("token quantity must greater than zero");
    }

    long quantityLimit = dynamicStore.getMarketQuantityLimit();
    if (sellTokenQuantity > quantityLimit || buyTokenQuantity > quantityLimit) {
      throw new ContractValidateException("token quantity must less than " + quantityLimit);
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L402-404)
```java
    long takerBuyTokenQuantityRemain = MarketUtils
        .multiplyAndDivide(takerSellRemainQuantity, makerSellQuantity, makerBuyQuantity,
            this.disableJavaLangMath());
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L426-428)
```java
      makerBuyTokenQuantityReceive = MarketUtils
          .multiplyAndDivide(makerSellRemainQuantity, makerBuyQuantity, makerSellQuantity,
              this.disableJavaLangMath());
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L461-463)
```java
      makerBuyTokenQuantityReceive = MarketUtils
          .multiplyAndDivide(makerSellRemainQuantity, makerBuyQuantity, makerSellQuantity,
              this.disableJavaLangMath());
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L538-562)
```java
  private void addTrxOrToken(MarketOrderCapsule orderCapsule, long num,
      AccountCapsule accountCapsule) {

    byte[] buyTokenId = orderCapsule.getBuyTokenId();
    if (Arrays.equals(buyTokenId, "_".getBytes())) {
      accountCapsule.setBalance(addExact(accountCapsule.getBalance(), num));
    } else {
      accountCapsule
          .addAssetAmountV2(buyTokenId, num, dynamicStore, assetIssueStore);
    }
  }

  private void addTrxOrToken(MarketOrderCapsule orderCapsule, long num) {
    AccountCapsule accountCapsule = accountStore
        .get(orderCapsule.getOwnerAddress().toByteArray());

    byte[] buyTokenId = orderCapsule.getBuyTokenId();
    if (Arrays.equals(buyTokenId, "_".getBytes())) {
      accountCapsule.setBalance(addExact(accountCapsule.getBalance(), num));
    } else {
      accountCapsule
          .addAssetAmountV2(buyTokenId, num, dynamicStore, assetIssueStore);
    }
    accountStore.put(orderCapsule.getOwnerAddress().toByteArray(), accountCapsule);
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L215-224)
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/SafeExchangeProcessor.java (L30-38)
```java
  private long exchangeFromSupply(long balance, BigDecimal supplyQuant) {
    BigDecimal bdBalance = BigDecimal.valueOf(balance);
    BigDecimal base = BigDecimal.ONE.add(
        supplyQuant.divide(SUPPLY, 18, RoundingMode.HALF_UP));
    double powResult = StrictMathWrapper.pow(base.doubleValue(), 2000.0);
    BigDecimal exchangeBalance = bdBalance.multiply(
        BigDecimal.valueOf(powResult).subtract(BigDecimal.ONE));
    return exchangeBalance.setScale(0, RoundingMode.DOWN).longValueExact();
  }
```
