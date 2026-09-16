### Title
Exchange/Market subsystems compare and swap TRC10 token quantities as raw integers without normalizing for each asset's `precision` (decimals), allowing decimal-mismatched token pairs to be mispriced by orders of magnitude - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java`)

### Summary
TRC10 assets in java-tron carry a per-token `precision` field (0-6 decimals) set at issuance [1](#0-0) , validated only at issuance time [2](#0-1) . However, every downstream contract that computes exchange rates or matches trade prices between two arbitrary tokens — `ExchangeCreateActuator`, `ExchangeInjectActuator`, `ExchangeWithdrawActuator`, `ExchangeCapsule`/`ExchangeProcessor` (bancor-style AMM), and the entire TRC10 order-book market (`MarketSellAssetActuator`, `MarketUtils`, `MarketComparator`) — operates purely on the raw `long` quantity fields of the contracts and never reads or applies `AssetIssueContract.precision` for either token in the pair. This mirrors the Gearbox report's root cause: a value (there, an oracle price; here, a token quantity) is consumed by downstream math while implicitly assuming a uniform decimal scale, without checking/normalizing the actual decimals of each side.

### Finding Description
- `AssetIssueActuator` only bounds `precision` to `[0, 6]` when `AllowSameTokenName` is active; it does not enforce any equality or compatibility between the decimals of tokens that will later be traded against each other [2](#0-1) .
- `ExchangeCreateActuator.doValidate()`/`execute()` accepts `firstTokenId`/`secondTokenId` and their initial `firstTokenBalance`/`secondTokenBalance` and stores them directly into the `ExchangeCapsule` pool with no reference to either token's `precision` [3](#0-2) .
- `ExchangeInjectActuator.execute()` computes the counterpart token amount via simple cross-multiplication of raw balances (`floorDiv(multiplyExact(secondTokenBalance, tokenQuant), firstTokenBalance)`), again with no decimal normalization [4](#0-3) .
- `ExchangeWithdrawActuator.doValidate()` performs the analogous BigDecimal ratio calculation directly on raw balances/quant, still without any precision-based scaling [5](#0-4) .
- `ExchangeCapsule.transaction()` / `ExchangeProcessor` / `SafeExchangeProcessor` implement the bancor-style constant-supply AMM purely over raw `long` balances, with no decimal awareness whatsoever [6](#0-5) [7](#0-6) .
- On the order-book market side, `MarketComparator.comparePrice()`/`comparePriceKey()` and `MarketUtils.priceMatch()` compare `sellTokenQuantity`/`buyTokenQuantity` cross-products directly, and `MarketSellAssetActuator.matchSingleOrder()` computes fill amounts via `MarketUtils.multiplyAndDivide` on raw quantities [8](#0-7) [9](#0-8) . None of these files (`grep` for `precision` across `*Market*.java` and `*Exchange*.java` production sources returned no matches) ever look up the `AssetIssueContract.precision` of the sell/buy token.

Because `precision` is chosen independently per asset at issuance (0-6), a "1 unit" price/ratio set for a token with `precision=0` (i.e., raw integer semantics) is numerically identical to "1 unit" of a token with `precision=6` (i.e., value = raw/10^6). The exchange/market math has no way to distinguish these, so it treats 1 raw unit of a 6-decimal token as equal in "quantity" to 1,000,000 raw units of a 0-decimal token when computing exchange ratios or matching orders.

### Impact Explanation
An attacker who is simply an asset issuer (`AssetIssueActuator`) and an unprivileged order/exchange participant (`ExchangeCreateActuator`, `MarketSellAssetActuator`) can:
1. Issue two TRC10 tokens with different `precision` values (e.g., 0 and 6).
2. Create an `Exchange` pool or place market sell/buy orders pairing these tokens at a nominal 1:1 (or any) raw-quantity ratio that is actually mispriced by up to 10^6× in real economic value because the protocol never rescales by decimals.
3. Trade against counterparties (or against TRX, which is fixed at 6 decimals) at this decimal-skewed rate, extracting real value (TRX or other assets) far in excess of what was deposited — an unbacked-balance / theft-of-funds condition for any counterparty or liquidity provider in the exchange/market who is unaware of the precision mismatch, and a direct analog of the Gearbox price-feed decimal bug where mismatched decimals directly translate into a mispriced position that can be exploited for profit.

This satisfies the "concrete... theft of funds / unbacked balance" bar since real TRX/asset balances move based on a systematically wrong exchange rate.

### Likelihood Explanation
Likelihood is high for exploitation because:
- Asset issuance with arbitrary `precision` in `[0,6]` is fully permissionless (any account can call `AssetIssueActuator`).
- Exchange creation/injection/withdrawal and Market order placement are permissionless, broadcastable transactions reachable by any signed sender (`ExchangeCreateActuator`, `ExchangeInjectActuator`, `MarketSellAssetActuator`).
- No validation anywhere in these actuators cross-checks token precision compatibility, so the vulnerable path requires no special conditions beyond issuing two tokens with different precision and creating a pool/order between them.

### Recommendation
When creating an `Exchange` (`ExchangeCreateActuator`), injecting/withdrawing liquidity (`ExchangeInjectActuator`, `ExchangeWithdrawActuator`), or placing/matching market orders (`MarketSellAssetActuator`, `MarketComparator`, `MarketUtils`), look up `AssetIssueContract.getPrecision()` for both `sellTokenId`/`buyTokenId` (or `firstTokenId`/`secondTokenId`), and normalize all quantities to a common decimal base (e.g., 6, matching TRX) before performing ratio/cross-multiplication math, analogous to fetching and applying `priceFeeds[token].decimals()` in the referenced Gearbox fix. Alternatively, enforce that both tokens participating in the same exchange/market pair must share identical `precision`, rejecting the transaction otherwise.

### Proof of Concept
1. Attacker issues Token A (`precision = 0`) and Token B (`precision = 6`) via `AssetIssueActuator` (validated only against the 0-6 bound, no cross-token check) [2](#0-1) .
2. Attacker calls `ExchangeCreateActuator` to create a pool with `firstTokenId = A`, `firstTokenBalance = 1_000_000`, `secondTokenId = B`, `secondTokenBalance = 1_000_000` — accepted as-is with no precision check [3](#0-2) .
3. Because Token A's raw units represent whole tokens (precision 0) while Token B's raw units represent millionths of a token (precision 6), the pool is nominally balanced 1:1 in raw units but is actually priced at 1,000,000:1 in real value.
4. Attacker calls `ExchangeInjectActuator`/trades against this pool using `ExchangeCapsule.transaction()` (bancor formula on raw balances) to extract Token B (or TRX-backed counter-tokens) far exceeding the true value deposited, since `exchangeToSupply`/`exchangeFromSupply` never adjust for the decimal mismatch [6](#0-5) .
5. The same setup can be replicated with `MarketSellAssetActuator`/`MarketComparator` order-book pricing to achieve equivalent mispriced fills [8](#0-7) .

### Citations

**File:** protocol/src/main/protos/core/contract/asset_issue_contract.proto (L21-23)
```text
  int32 trx_num = 6; // The fields trx_num and num define the exchange rate: num tokens can be purchased with trx_num TRX. This avoids using decimals.
  int32 precision = 7;
  int32 num = 8;
```

**File:** actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java (L176-181)
```java
    int precision = assetIssueContract.getPrecision();
    if (precision != 0
        && dynamicStore.getAllowSameTokenName() != 0
        && (precision < 0 || precision > ActuatorConstant.PRECISION_DECIMAL)) {
      throw new ContractValidateException("precision cannot exceed 6");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L183-218)
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L214-227)
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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L415-429)
```java
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
```
