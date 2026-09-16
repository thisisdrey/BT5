## Title
Integer overflow in `MarketUtils.multiplyAndDivide` BigInteger fallback silently truncates order-matching quantities via `.longValue()` instead of `.longValueExact()` - ([File: chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java])

### Summary
`MarketUtils.multiplyAndDivide(a, b, c, disableMath)` is used by `MarketSellAssetActuator.matchSingleOrder` to compute the token quantities exchanged between a taker and a maker order in the TRC10 exchange/market. The fast path uses `multiplyExact`/`floorDiv`, which correctly throws `ArithmeticException` on overflow. But the fallback path, entered specifically when that overflow occurs, converts the `BigInteger` result back to a primitive `long` with `.longValue()` rather than `.longValueExact()`: [1](#0-0) 

`.longValue()` silently truncates/wraps modulo 2^64 instead of throwing when the mathematically correct `a*b/c` result does not fit in a `long`. This is the same bug class as CVE-2019-5827 (an integer-overflow path that is reached specifically because the value is too large, and where the overflow handling itself is broken, corrupting subsequent state instead of failing safely).

### Finding Description
`multiplyAndDivide` is the core price-matching primitive for TRON's on-chain market/exchange (`MarketSellAssetActuator`), used in `matchSingleOrder`: [2](#0-1) [3](#0-2) [4](#0-3) 

`sellTokenQuantity` and `buyTokenQuantity` for both the taker's and any resting maker's order come directly from `MarketSellAssetContract` fields under attacker control (both fields are plain `long`/`int64` protobuf fields with no explicit upper bound enforced against `Long.MAX_VALUE` visible in the actuator): [5](#0-4) 

Given two attacker-controlled `long` quantities near `Long.MAX_VALUE` for a maker order and a taker order, `a*b` can reach ~2^126, well beyond the ~2^63 range that fits back into a `long` even after dividing by `c`. In that scenario `multiplyExact` throws inside `multiplyAndDivide`, the code falls into the `catch` block, and the `BigInteger` result — still larger than `Long.MAX_VALUE` — is truncated via `.longValue()`, which silently returns an arbitrary (potentially negative or small, incorrect) value instead of throwing.

This differs sharply from the rest of the codebase, which the repository has otherwise hardened extensively against exactly this failure mode: `RepositoryImpl`, `ResourceProcessor`, `EnergyProcessor`, `ExchangeInjectActuator`/`ExchangeWithdrawActuator` (which use `.longValueExact()` and `StrictMathWrapper.addExact/subtractExact`), and `AssetIssueActuator`/`ParticipateAssetIssueActuator` (explicit "long overflow" tests) all convert overflow conditions into thrown exceptions rather than silent truncation: [6](#0-5) [7](#0-6) 

`MarketUtils.multiplyAndDivide` is the one arithmetic path in this class of code that still uses non-exact `BigInteger.longValue()`, breaking the "fail closed" pattern used everywhere else.

### Impact Explanation
If the truncated/wrapped quantity is used to set `takerBuyTokenQuantityReceive`/`makerBuyTokenQuantityReceive` and subsequently credited to an account's asset balance or bandwidth/TRX balance via `MarketOrderCapsule`/`AccountCapsule` asset updates, a maliciously crafted maker+taker order pair with sufficiently large (but individually valid, non-overflowing on their own) `sellTokenQuantity`/`buyTokenQuantity` values could cause the matched trade quantity to become an arbitrary, attacker-influenced wrapped value (potentially large or even negative before being coerced), leading to incorrect token crediting — i.e., an unbacked balance / theft of asset tokens from the market pool, or corruption of order-quantity accounting. This satisfies the required impact bar of "unbacked balance"/"theft of funds" for TRC10 assets traded via the on-chain market.

### Likelihood Explanation
Reaching the vulnerable path requires an attacker (or attacker plus a cooperating/self-controlled counter-order) to place market orders with extremely large `sellTokenQuantity`/`buyTokenQuantity` (on the order of `Long.MAX_VALUE`), which is only gated by the actuator's balance/asset-sufficiency checks, not an explicit sane bound on quantity magnitude relative to `Long.MAX_VALUE / Long.MAX_VALUE` products. Because TRC10 asset balances are also `long`, an attacker would need to actually hold (or have issued) a token with a supply large enough to construct such an order — this is a meaningful precondition that narrows likelihood, since it is not achievable with a trivial/near-zero balance. Full confirmation of exact reachability (e.g., whether balance-sufficiency checks in `MarketSellAssetActuator.validate()` cap quantities below a value that could trigger the multiply overflow) was not completed within available search iterations — the validate() method contents beyond the visible range were not fully retrieved. This is noted as an open item requiring further verification.

### Recommendation
Change `MarketUtils.multiplyAndDivide`'s fallback to use `longValueExact()` instead of `longValue()`, and propagate `ArithmeticException` up so callers reject the transaction/order match rather than silently using a wrapped value:
```java
return aBig.multiply(bBig).divide(cBig).longValueExact();
```
Additionally, callers such as `MarketSellAssetActuator.matchSingleOrder` should catch/handle the resulting `ArithmeticException` explicitly (fail the match / reject order) instead of allowing an unchecked exception to propagate unexpectedly, consistent with the exception handling pattern already used in `ExchangeInjectActuator`/`ExchangeWithdrawActuator`.

### Proof of Concept
Conceptual PoC (not fully verified against `validate()` bound checks, see Likelihood section):
1. Issue/acquire a TRC10 asset with a supply large enough to place a sell order with `sellTokenQuantity` and `buyTokenQuantity` near `Long.MAX_VALUE`.
2. Place a maker `MarketSellAssetContract` order for pair (A, TRX) with `sellTokenQuantity = X`, `buyTokenQuantity = Y` where X, Y are large.
3. Place a taker order for the opposite side with quantities chosen so that, inside `matchSingleOrder`, the internal call `MarketUtils.multiplyAndDivide(a, b, c, ...)` computes `a*b` that overflows `long` (triggering the `ArithmeticException` in `multiplyExact`), forcing execution into the `BigInteger` fallback, while the true mathematical result of `a*b/c` still exceeds `Long.MAX_VALUE`.
4. Observe that `.longValue()` returns a wrapped/truncated result instead of throwing, and that this incorrect quantity is applied to update order and account asset balances, producing an inconsistent/unbacked balance state. [1](#0-0) [8](#0-7)

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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L78-120)
```java
  private byte[] sellTokenID = null;
  private byte[] buyTokenID = null;
  private long sellTokenQuantity;
  private long buyTokenQuantity;

  public MarketSellAssetActuator() {
    super(ContractType.MarketSellAssetContract, MarketSellAssetContract.class);
  }

  private void initStores() {
    accountStore = chainBaseManager.getAccountStore();
    dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    assetIssueStore = chainBaseManager.getAssetIssueStore();
    assetIssueV2Store = chainBaseManager.getAssetIssueV2Store();

    marketAccountStore = chainBaseManager.getMarketAccountStore();
    orderStore = chainBaseManager.getMarketOrderStore();
    pairToPriceStore = chainBaseManager.getMarketPairToPriceStore();
    pairPriceToOrderStore = chainBaseManager.getMarketPairPriceToOrderStore();
  }

  @Override
  public boolean execute(Object object) throws ContractExeException {
    initStores();

    TransactionResultCapsule ret = (TransactionResultCapsule) object;
    if (Objects.isNull(ret)) {
      throw new RuntimeException(TX_RESULT_NULL);
    }

    long fee = calcFee();

    try {
      final MarketSellAssetContract contract = this.any
          .unpack(MarketSellAssetContract.class);

      AccountCapsule accountCapsule = accountStore
          .get(contract.getOwnerAddress().toByteArray());

      sellTokenID = contract.getSellTokenId().toByteArray();
      buyTokenID = contract.getBuyTokenId().toByteArray();
      sellTokenQuantity = contract.getSellTokenQuantity();
      buyTokenQuantity = contract.getBuyTokenQuantity();
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L382-484)
```java
  // return all match or not
  private void matchSingleOrder(MarketOrderCapsule takerOrderCapsule,
      MarketOrderCapsule makerOrderCapsule, TransactionResultCapsule ret,
      AccountCapsule takerAccountCapsule)
      throws ItemNotFoundException {

    long takerSellRemainQuantity = takerOrderCapsule.getSellTokenQuantityRemain();
    long makerSellQuantity = makerOrderCapsule.getSellTokenQuantity();
    long makerBuyQuantity = makerOrderCapsule.getBuyTokenQuantity();
    long makerSellRemainQuantity = makerOrderCapsule.getSellTokenQuantityRemain();

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

    if (takerBuyTokenQuantityRemain == 0) {
      // quantity too small, return sellToken to user
      takerOrderCapsule.setSellTokenQuantityReturn();
      MarketUtils.returnSellTokenRemain(takerOrderCapsule, takerAccountCapsule,
          dynamicStore, assetIssueStore);
      MarketUtils.updateOrderState(takerOrderCapsule, State.INACTIVE, marketAccountStore);
      return;
    }

    long takerBuyTokenQuantityReceive; // In this match, the token obtained by taker
    long makerBuyTokenQuantityReceive; // the token obtained by maker

    if (takerBuyTokenQuantityRemain == makerOrderCapsule.getSellTokenQuantityRemain()) {
      // taker == maker

      // makerSellTokenQuantityRemain_A/makerBuyTokenQuantityCurrent_TRX =
      //   makerSellTokenQuantity_A/makerBuyTokenQuantity_TRX
      // => makerBuyTokenQuantityCurrent_TRX = makerSellTokenQuantityRemain_A *
      //   makerBuyTokenQuantity_TRX / makerSellTokenQuantity_A

      makerBuyTokenQuantityReceive = MarketUtils
          .multiplyAndDivide(makerSellRemainQuantity, makerBuyQuantity, makerSellQuantity,
              this.disableJavaLangMath());
      takerBuyTokenQuantityReceive = makerOrderCapsule.getSellTokenQuantityRemain();

      long takerSellTokenLeft =
          takerOrderCapsule.getSellTokenQuantityRemain() - makerBuyTokenQuantityReceive;
      takerOrderCapsule.setSellTokenQuantityRemain(takerSellTokenLeft);
      makerOrderCapsule.setSellTokenQuantityRemain(0);

      if (takerSellTokenLeft == 0) {
        MarketUtils.updateOrderState(takerOrderCapsule, State.INACTIVE, marketAccountStore);
      }
      MarketUtils.updateOrderState(makerOrderCapsule, State.INACTIVE, marketAccountStore);
    } else if (takerBuyTokenQuantityRemain < makerOrderCapsule.getSellTokenQuantityRemain()) {
      // taker < maker
      // if the quantity of taker want to buy is smaller than the remain of maker want to sell,
      // consume the order of the taker

      takerBuyTokenQuantityReceive = takerBuyTokenQuantityRemain;
      makerBuyTokenQuantityReceive = takerOrderCapsule.getSellTokenQuantityRemain();

      takerOrderCapsule.setSellTokenQuantityRemain(0);
      MarketUtils.updateOrderState(takerOrderCapsule, State.INACTIVE, marketAccountStore);

      makerOrderCapsule.setSellTokenQuantityRemain(subtractExact(
          makerOrderCapsule.getSellTokenQuantityRemain(), takerBuyTokenQuantityRemain));
    } else {
      // taker > maker
      takerBuyTokenQuantityReceive = makerOrderCapsule.getSellTokenQuantityRemain();

      // if the quantity of taker want to buy is bigger than the remain of maker want to sell,
      // consume the order of maker
      // makerSellTokenQuantityRemain_A/makerBuyTokenQuantityCurrent_TRX =
      //   makerSellTokenQuantity_A/makerBuyTokenQuantity_TRX
      makerBuyTokenQuantityReceive = MarketUtils
          .multiplyAndDivide(makerSellRemainQuantity, makerBuyQuantity, makerSellQuantity,
              this.disableJavaLangMath());

      MarketUtils.updateOrderState(makerOrderCapsule, State.INACTIVE, marketAccountStore);
      if (makerBuyTokenQuantityReceive == 0) {
        // the quantity is too small, return the remain of sellToken to maker
        // it would not happen here
        // for the maker, when sellQuantity < buyQuantity, it will get at least one buyToken
        // even when sellRemain = 1.
        // so if sellQuantity=200，buyQuantity=100, when sellRemain=1, it needs to be satisfied
        // the following conditions:
        // makerOrderCapsule.getSellTokenQuantityRemain() - takerBuyTokenQuantityRemain = 1
        // 200 - 200/100 * X = 1 ===> X = 199/2，and this comports with the fact that X is integer.
        makerOrderCapsule.setSellTokenQuantityReturn();
        returnSellTokenRemain(makerOrderCapsule);
        return;
      } else {
        makerOrderCapsule.setSellTokenQuantityRemain(0);
        takerOrderCapsule.setSellTokenQuantityRemain(subtractExact(
            takerOrderCapsule.getSellTokenQuantityRemain(), makerBuyTokenQuantityReceive));
      }
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

**File:** chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java (L371-378)
```java
  protected long calculateGlobalLimitV2(long frozeBalance,
      long totalLimit, long totalWeight) {
    return BigInteger.valueOf(frozeBalance)
        .multiply(BigInteger.valueOf(totalLimit))
        .divide(BigInteger.valueOf(TRX_PRECISION)
            .multiply(BigInteger.valueOf(totalWeight)))
        .longValueExact();
  }
```
