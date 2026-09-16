### Title
Silent BigInteger-to-long truncation in `MarketUtils.multiplyAndDivide` fallback path allows loss-of-precision in Market order matching - ([File: chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java])

### Summary
`MarketUtils.multiplyAndDivide(long a, long b, long c, boolean disableMath)` first tries an overflow-checked `multiplyExact`/`floorDiv` path, and on `ArithmeticException` falls back to `BigInteger` arithmetic that ends with `.longValue()` instead of `.longValueExact()`, silently truncating/wrapping any result that does not fit in a `long`. This mirrors the reported bug class in the external report (`PackedFixed18Lib.unpack` casting an out-of-range value into a narrower type via an unchecked cast, silently truncating instead of reverting), applied here to a fund-amount computation reachable from an on-chain, user-broadcast transaction.

### Finding Description
`multiplyAndDivide` is defined at [1](#0-0) . When `multiplyExact`/`floorDiv` overflow (a legitimate case, since `a * b` can exceed `Long.MAX_VALUE` for large sell/buy quantities), the code recomputes with `BigInteger` but converts the final quotient back to `long` using `longValue()`, which per the Java spec narrows by discarding all but the low-order 64 bits — i.e., exactly the same "cast outside the representable range causes truncation" defect described in the `PackedFixed18Lib.unpack` report (which recommended using an exact/checked conversion instead of a blind narrowing cast).

This function is called directly from the actuator that processes a user-broadcastable, permissionless transaction type (`MarketSellAssetContract`), during order matching in `MarketSellAssetActuator.matchSingleOrder`: [2](#0-1) [3](#0-2) [4](#0-3) 

The result of `multiplyAndDivide` (`takerBuyTokenQuantityRemain` / `makerBuyTokenQuantityReceive`) directly drives token/TRX crediting via `addTrxOrToken` (`accountCapsule.setBalance(addExact(...))` or `accountCapsule.addAssetAmountV2(...)`) at [5](#0-4)  and debiting of the maker/taker remaining sell quantities used in subsequent `subtractExact` calls at [6](#0-5) . Validation caps `sellTokenQuantity`/`buyTokenQuantity` per-order to `dynamicStore.getMarketQuantityLimit()` at [7](#0-6) , but this cap only bounds a single order's own quantities, not the arithmetic across matched maker/taker orders (`a * b` uses `makerSellQuantity`/`makerBuyQuantity` of pre-existing resting orders combined with the new taker's remaining quantity) — so whether the multiply can actually overflow `Long.MAX_VALUE` (~9.22e18) in a way that also overflows `long` after `BigInteger` division depends on the configured `MarketQuantityLimit` and the specific maker book state, which I could not fully verify from the available files (the exact deployed value of `getMarketQuantityLimit()` and the possible combinations of resting maker orders were not inspectable in this session).

### Impact Explanation
If reachable, silent truncation of `takerBuyTokenQuantityReceive` / `makerBuyTokenQuantityReceive` would desynchronize the amount credited to an account balance/asset from the amount actually consumed from the counterparty's remaining order quantity (since `subtractExact` on the *un-truncated* conceptual value is not what's used — the truncated value is used consistently within a single call, but the truncation itself represents a mismatch between the intended mathematical trade ratio and the credited amount), constituting an unbacked balance / incorrect fund accounting bug in the on-chain exchange logic reachable by a normal user transaction.

### Likelihood Explanation
Low-to-uncertain. This requires a `BigInteger` product `a*b` whose true quotient by `c` still exceeds `Long.MAX_VALUE`/underflows `Long.MIN_VALUE`, which requires extreme quantities in maker/taker orders. Given per-order validation caps quantities via `getMarketQuantityLimit()`, actually triggering this within `multiplyAndDivide`'s fallback truncation may not be practically achievable under production limits — I could not confirm the configured limit value or find a concrete reachable combination that defeats it, so likelihood cannot be confirmed as concrete/high from the code inspected.

### Recommendation
Replace `.longValue()` with `.longValueExact()` in `MarketUtils.multiplyAndDivide` (chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java:276) so that an out-of-range result throws `ArithmeticException` (already handled/caught by callers such as `MarketSellAssetActuator.validate()`/`execute()`) rather than silently wrapping, consistent with how `ExchangeWithdrawActuator` already uses `longValueExact()` for analogous BigInteger-based ratio computations (actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java:80,86).

### Proof of Concept
Not independently reproduced in this session — I could not confirm concrete `a, b, c` values reachable through `MarketSellAssetActuator` (bounded by `getMarketQuantityLimit()`) that cause the `BigInteger` quotient to exceed the `long` range while still passing all validation. A background agent with test-execution access should attempt to construct maker/taker orders with quantities near the configured `getMarketQuantityLimit()` on both sides of `matchSingleOrder`'s `multiplyAndDivide` calls to determine whether truncation is achievable in practice.

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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L227-230)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L450-452)
```java

      makerOrderCapsule.setSellTokenQuantityRemain(subtractExact(
          makerOrderCapsule.getSellTokenQuantityRemain(), takerBuyTokenQuantityRemain));
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L461-463)
```java
      makerBuyTokenQuantityReceive = MarketUtils
          .multiplyAndDivide(makerSellRemainQuantity, makerBuyQuantity, makerSellQuantity,
              this.disableJavaLangMath());
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L538-548)
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
```
