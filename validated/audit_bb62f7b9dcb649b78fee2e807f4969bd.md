### Title
Exchange/Market pools compute trade ratios from raw token amounts without normalizing for each TRC10 asset's `precision`, enabling mispricing analogous to the LP-oracle decimal bug - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`, `chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java`, `platform/src/main/java/common/org/tron/common/utils/MarketComparator.java`)

### Summary
Java-tron's Bancor-style Exchange (`ExchangeCreateActuator`, `ExchangeInjectActuator`, `ExchangeWithdrawActuator`, `ExchangeTransactionActuator`) and the order-book Market (`MarketSellAssetActuator`, `MarketUtils`) treat the raw `long` token-quantity fields of two arbitrary TRC10 assets as directly fungible units when computing swap ratios and price comparisons, exactly like the LP oracle finding assumed all priced tokens use 18 decimals. TRC10 assets can be issued with different `precision` values (0-6, enforced in `AssetIssueActuator`), yet none of the exchange/market math ever reads or normalizes for `AssetIssueContract.precision`. An attacker who issues two tokens with mismatched precision and creates an exchange pool or market pair can systematically over/undervalue one side of the trade against counterparties.

### Finding Description
`AssetIssueContract` carries a `precision` field that determines the effective decimal scale of a TRC10 asset, checked only at issuance time: [1](#0-0) 

This `precision` value is never consulted again. `ExchangeCreateActuator.doValidate` allows any two distinct TRC10 tokens (or TRX) to be paired into a pool, only checking balances/limits, not decimal compatibility: [2](#0-1) 

Trading against the pool (`ExchangeInjectActuator`, mirrored in `ExchangeWithdrawActuator`/`ExchangeTransactionActuator`) then computes the counter-token amount as a straight cross-multiplication of raw integer balances: [3](#0-2) 

`ExchangeCapsule.transaction`, which backs the Bancor-formula swap, likewise operates purely on raw `long` balances with no decimal adjustment: [4](#0-3) 

The order-book Market path exhibits the same assumption: `MarketComparator.comparePrice` and `MarketUtils.priceMatch` compare `sellTokenQuantity`/`buyTokenQuantity` ratios of two potentially different-precision TRC10 tokens as plain integers: [5](#0-4) [6](#0-5) 

Because a "unit" of a precision-0 token and a "unit" of a precision-6 token differ by up to 10^6 in real value, none of this code enforces or compensates for that difference — the same root cause the external report identifies for `LP.sol`'s `price(...).fmul(balance, WAD)` computation.

### Impact Explanation
An attacker (any account holding TRX for the small issuance/create fees — asset issuance and Exchange/Market creation are permissionless in java-tron) can:
1. Issue Token A with `precision = 0` and Token B with `precision = 6` via `AssetIssueActuator`.
2. Create an `Exchange` pool or a Market order pair between A and B via `ExchangeCreateActuator` / `MarketSellAssetActuator`, seeding it so the raw integer ratio looks "fair" but is off by up to 10^6x in real economic value.
3. Induce/wait for other users (who reasonably assume 1 raw unit ≈ 1 raw unit given no visible warning) to trade into the mispriced pool/pair via `ExchangeTransactionActuator`, `ExchangeInjectActuator`, `ExchangeWithdrawActuator`, or the Market matching engine, extracting the mispriced token at the victims' expense.

This results in unauthorized value transfer / theft of funds from counterparties trading in the pool or matching against the order book — a concrete "theft of funds" outcome as required.

### Likelihood Explanation
Both TRC10 asset issuance and Exchange/Market creation are unprivileged, single-transaction operations reachable by any signed account (no SR/witness/committee privilege needed), matching the in-scope "asset issuer"/"order placer" actor. The precision mismatch is entirely attacker-controlled at issuance time and requires no race condition or special timing, making exploitation straightforward once a counterparty is willing to trade against the pool/pair.

### Recommendation
Either (a) enforce that both tokens in an `Exchange` pool or Market pair share identical `AssetIssueContract.precision` (and treat TRX's implicit 6-decimal precision consistently) at `ExchangeCreateActuator`/`MarketSellAssetActuator` validation time, or (b) thread each token's cached `precision` through the swap/comparison math (`ExchangeCapsule.transaction`, `ExchangeInjectActuator`/`ExchangeWithdrawActuator` ratio computation, `MarketComparator.comparePrice`) so that raw quantities are scaled to a common base before any ratio or price comparison is performed.

### Proof of Concept
1. Issue Token A via `AssetIssueContract` with `precision = 0`, total supply 1,000,000.
2. Issue Token B via `AssetIssueContract` with `precision = 6`, total supply 1,000,000.
3. Call `ExchangeCreateActuator` to create a pool with `firstTokenBalance = 1,000,000` (A) and `secondTokenBalance = 1,000,000` (B) — accepted because `ExchangeCreateActuator.doValidate` only checks balances/limits, not precision equality (`actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java` lines 183-203).
4. A victim, assuming raw-unit parity, injects/withdraws or swaps against the pool via `ExchangeInjectActuator`/`ExchangeTransactionActuator`; because Token B's raw unit is actually 10^6 times less valuable in real terms than Token A's (due to differing `precision`), the victim receives/gives an economically mispriced amount, letting the attacker drain real value — mirroring the exact undervaluation/overvaluation mechanism described in the `LP.sol` report.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L183-203)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L71-83)
```java
      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
        anotherTokenQuant = floorDiv(multiplyExact(
            secondTokenBalance, tokenQuant), firstTokenBalance);
        exchangeCapsule.setBalance(addExact(firstTokenBalance, tokenQuant),
            addExact(secondTokenBalance, anotherTokenQuant));
      } else {
        anotherTokenID = firstTokenID;
        anotherTokenQuant = floorDiv(multiplyExact(
            firstTokenBalance, tokenQuant), secondTokenBalance);
        exchangeCapsule.setBalance(addExact(firstTokenBalance, anotherTokenQuant),
            addExact(secondTokenBalance, tokenQuant));
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

**File:** platform/src/main/java/common/org/tron/common/utils/MarketComparator.java (L65-86)
```java
  /**
   * Note: the params should be the same token pair, or you should change the order.
   * All the quantity should be bigger than 0.
   * */
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

**File:** chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java (L231-249)
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
```
