### Title
Undocumented decimal/precision assumption in TRC10 Market order matching leads to unfair pricing and value loss - (File: actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java)

### Summary
`MarketSellAssetActuator` (reachable via the `MarketSellAssetContract` broadcast by any account) computes and matches order prices purely from raw `sellTokenQuantity`/`buyTokenQuantity` integers, with no reference to each TRC10 token's `precision` field defined in `asset_issue_contract.proto` and exposed via `AssetIssueCapsule.getPrecision()`. This mirrors the Chainlink `M03` report's root cause: silently assuming all inputs share a common decimal base, when in the actual data model tokens can be issued with different `precision` values.

### Finding Description
- The market price for an order pair is derived directly from the integer `sellTokenQuantity`/`buyTokenQuantity` ratio, reduced by GCD, with no scaling factor for token precision: [1](#0-0) 
- Price comparisons between taker/maker orders (`priceMatch`) and matching math (`multiplyAndDivide`) operate on these raw quantities as if they are all expressed in the same unit: [2](#0-1) 
- `MarketSellAssetActuator.validate()` checks that the sell/buy asset IDs exist and that quantities are positive and below `quantityLimit`, but never reads or compares the `precision` field of the involved `AssetIssueCapsule`s, nor documents that callers must normalize quantities by token precision before submitting an order: [3](#0-2) 
- `AssetIssueCapsule` explicitly exposes a `precision` accessor, confirming TRC10 tokens are permitted to have differing decimal precision, yet nothing in the market actuator or `MarketUtils` price-key/matching logic consumes it. The actual order-matching arithmetic (`matchSingleOrder`) further compounds this by doing integer multiply/divide across sell/buy quantities of two different tokens without any precision-aware scaling: [4](#0-3) 

Just as the Chainlink `singlePrice`/`doublePrice` functions silently assumed a fixed number of oracle decimals and amount decimals, java-tron's on-chain market silently assumes both TRC10 tokens in a pair use the same implicit unit scale (raw integer quantity), when in reality each TRC10 asset can be issued with an independent `precision` (decimal places) value that the market layer never inspects.

### Impact Explanation
An asset issuer can create two TRC10 tokens with different `precision` values (e.g., one with precision 0 and one with precision 6). Any user placing/matching orders through `MarketSellAssetContract` between such tokens will get a price ratio computed purely from raw integer quantities, off by orders of magnitude (10^precision-difference) from the token's real intended value. Because trade execution, balance transfers, and price-book insertion (`createPairPriceKey`, `priceMatch`) all use this unscaled ratio, a party unaware of the precision mismatch can be tricked into selling/buying at a massively unfavorable effective price, resulting in direct loss of funds for the mismatched-precision counterpart, or a market that structurally misprices decimal-heterogeneous token pairs. This is a fund-loss/mispricing issue reachable by any unprivileged transaction signer, matching the severity bar (concrete unauthorized value transfer / loss of funds).

### Likelihood Explanation
High likelihood of reachability: any account can issue a TRC10 asset with an arbitrary `precision` via `AssetIssueActuator`, and any account can submit `MarketSellAssetContract` orders once `AllowMarketTransaction` is enabled by the committee. No special privileges are needed beyond normal token issuance and order placement, both of which are standard, permissionless, unprivileged operations exposed through the public transaction/API surface (Wallet gRPC/HTTP `MarketSellAsset`).

### Recommendation
- Document explicitly (in `MarketSellAssetContract` and `MarketUtils`) that `sellTokenQuantity`/`buyTokenQuantity` are raw integer units, and require order placers to account for each token's `precision` themselves, or
- Have `MarketSellAssetActuator.validate()`/`execute()` read each token's `precision` via `AssetIssueCapsule.getPrecision()` and normalize quantities (or reject pairs with mismatched precision without an explicit scaling factor) before computing/storing price keys and before matching, ensuring price and matched-amount arithmetic is precision-consistent between the two legs of a pair.

### Proof of Concept
1. Issue TRC10 token `A` with `precision = 0` and token `B` with `precision = 6` via `AssetIssueActuator` (permissionless, standard asset issuance).
2. Enable `AllowMarketTransaction` (assume already enabled on a live network).
3. Submit `MarketSellAssetContract` selling `1000` units of `A` for `1000` units of `B`. Because `B` has 6 decimals of precision while `A` has 0, `1000` raw units of `B` represents `0.001` "real" `B` tokens, whereas `1000` raw units of `A` represents `1000` "real" `A` tokens — a 1,000,000x mispricing that `MarketUtils.createPairPriceKey`/`priceMatch`/`matchSingleOrder` will treat as a straightforward 1:1 price ratio, matching unaware counterparties at this distorted rate and causing one side to lose the economic value of their asset.

### Citations

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

**File:** chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java (L231-277)
```java
  /**
   * if takerPrice >= makerPrice, return True
   * note: here are two different token pairs
   * firstly, we should change the token pair of taker to be the same with maker
   */
  public static boolean priceMatch(MarketPrice takerPrice, MarketPrice makerPrice) {
    // for takerPrice, buyToken is A,sellToken is TRX.
    // price_A_taker * buyQuantity_taker = Price_TRX * sellQuantity_taker
    // ==> price_A_taker = Price_TRX * sellQuantity_taker/buyQuantity_taker

    // price_A_taker must be greater or equal to price_A_maker
    // price_A_taker / price_A_maker >= 1
    // ==> Price_TRX * sellQuantity_taker/buyQuantity_taker >= Price_TRX * buyQuantity_maker/sellQuantity_maker
    // ==> sellQuantity_taker * sellQuantity_maker > buyQuantity_taker * buyQuantity_maker

    return MarketComparator.comparePrice(takerPrice.getBuyTokenQuantity(),
        takerPrice.getSellTokenQuantity(),
        makerPrice.getSellTokenQuantity(), makerPrice.getBuyTokenQuantity()) >= 0;
  }

  public static void updateOrderState(MarketOrderCapsule orderCapsule,
      State state, MarketAccountStore marketAccountStore) throws ItemNotFoundException {
    orderCapsule.setState(state);

    // remove from account order list
    if (state == State.INACTIVE || state == State.CANCELED) {
      MarketAccountOrderCapsule accountOrderCapsule = marketAccountStore
          .get(orderCapsule.getOwnerAddress().toByteArray());
      accountOrderCapsule.removeOrder(orderCapsule.getID());
      marketAccountStore.put(accountOrderCapsule.createDbKey(), accountOrderCapsule);
    }
  }

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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L241-278)
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
    } catch (ArithmeticException e) {
      logger.debug(e.getMessage(), e);
      throw new ContractValidateException(e.getMessage());
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
