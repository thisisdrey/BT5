### Title
Double-precision arithmetic in bancor-style exchange calculation lets an unprivileged trader receive more tokens than deserved via `ExchangeTransactionContract` — ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java])

### Summary
`ExchangeTransactionActuator` computes the token amount received in a TRC10 bancor-relay exchange via `ExchangeCapsule.transaction()`, which by default (when `allowStrictMath`/hardened calc are not both enabled) delegates to `ExchangeProcessor`. That processor performs the core relay-supply math using `double` arithmetic and `StrictMath.pow`, exactly the class of bug flagged in the report: computing a ratio/price with limited floating-point precision and then deriving a token-transfer amount from it, causing the amount actually credited to the trader to diverge from the mathematically correct amount — in the trader's favor when rounding/precision loss goes the wrong way.

### Finding Description
`ExchangeCapsule.transaction()` selects between the legacy floating point `ExchangeProcessor` and the exact `SafeExchangeProcessor` based on the `hardenedCalc` flag (`allowHarden()`), which is a configurable/proposal-gated dynamic property, not always active. [1](#0-0) 

When the legacy path is used, `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` compute the bancor relay math with `double` values and `Maths.pow(...)`, casting the final `double` result down to a `long` with `(long) issuedSupply` / `(long) exchangeBalance`: [2](#0-1) 

This is structurally identical to the reported bug class: a price/ratio is derived using a lossy numeric representation (here, IEEE-754 double instead of the correct big-decimal/big-integer arithmetic), and that lossy value is then used directly to compute the amount of a token to award to a caller. The project's own test suite confirms the two paths produce materially different results for the same inputs — `testStrictMath` in `ExchangeProcessorTest` explicitly asserts `Assert.assertNotEquals(anotherTokenQuant, result)` between non-strict-double and strict-double/BigDecimal paths, and a dedicated `SafeExchangeProcessor` (BigDecimal-based) was introduced specifically to eliminate this precision loss: [3](#0-2) [4](#0-3) 

Any unprivileged account can trigger this calculation directly by broadcasting an `ExchangeTransactionContract`, which calls `exchangeCapsule.transaction(tokenID, tokenQuant, dynamicStore.allowStrictMath(), allowHarden())` both in `validate()` (to check `tokenExpected`) and again in `execute()` to actually credit `anotherTokenQuant` to the caller's balance/asset: [5](#0-4) 

Unless `hardenedCalc` (`allowHardenExchangeCalculation`) is enabled network-wide via committee proposal, every trade through this actuator computes the payout with lossy double/`Math.pow` arithmetic rather than the exact `SafeExchangeProcessor`.

### Impact Explanation
Because the payout amount is derived from a double-precision power computation instead of exact arithmetic, the amount of the "another token" credited to the caller in `ExchangeTransactionActuator.execute()` can differ from the value that a correct/exact computation would produce. Given the project's own tests show the double and BigDecimal implementations diverge on identical inputs (`assertNotEquals`), a trader who structures trade sizes/pool ratios to land on inputs where floating-point rounding favors them could repeatedly extract slightly more of the counter-token than the bonding-curve math actually entitles them to, at the expense of the shared TRC10 exchange pool (draining value from other pool participants over many transactions) — directly analogous to the "more rewards than it should" precision-loss issue in the report.

### Likelihood Explanation
The vulnerable path is the default one: any account can call `ExchangeTransactionContract` against any TRC10 exchange pair, and the exact-arithmetic `SafeExchangeProcessor` is only used when the `allowHardenExchangeCalculation` dynamic property has been separately turned on by committee/proposal. No special privilege is required — a normal signed transaction from any account suffices to reach `ExchangeCapsule.transaction()` → `ExchangeProcessor.exchange()`.

### Recommendation
Make the exact (`SafeExchangeProcessor`, BigDecimal-based) computation the unconditional/default path for `ExchangeCapsule.transaction()` rather than gating it behind `allowHarden()`, or otherwise bound/round the double-based computation so that no configuration of pool balances and trade size permits the legacy floating-point path to return a value greater than the value the exact computation would produce. At minimum, add an invariant check (as already exists for the withdraw actuators' "Not precise enough" checks) that rejects/clamps a double-computed payout whenever it deviates from the BigDecimal-computed value by more than a negligible epsilon.

### Proof of Concept
1. Do not enable `allowHardenExchangeCalculation` (default network state), so `allowHarden()` is false.
2. Create/use an existing TRC10 exchange pool via `ExchangeCreateContract`.
3. Submit an `ExchangeTransactionContract` from an unprivileged account with a `token_id`/`quant`/`expected` chosen (analogous to the `testStrictMath` fixture data in `ExchangeProcessorTest`, e.g. balances `{4732214L, 2202692725330L}` with quant `29218L`) such that the legacy double-based `ExchangeProcessor.exchange()` yields a strictly larger result than `SafeExchangeProcessor.INSTANCE.exchange()` would for the same inputs — as demonstrated by the existing `testStrictMath` unit test which explicitly asserts the two results are unequal. [6](#0-5) 
4. `ExchangeTransactionActuator.execute()` credits `anotherTokenQuant` (the inflated, floating-point-derived amount) to the caller's account via `addAssetAmountV2`/balance update, while debiting the exchange pool by the same (excess) amount — extracting value from the shared pool beyond what the exact bonding-curve formula entitles the trader to. [7](#0-6)

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-146)
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

**File:** framework/src/test/java/org/tron/core/capsule/utils/ExchangeProcessorTest.java (L218-280)
```java
  @Test
  public void testStrictMath() {
    long supply = 1_000_000_000_000_000_000L;
    long[][] testData = {
        {4732214L, 2202692725330L, 29218L},
        {5618633L, 556559904655L, 1L},
        {9299554L, 1120271441185L, 7000L},
        {62433133L, 12013267997895L, 100000L},
        {64212664L, 725836766395L, 50000L},
        {64126212L, 2895100109660L, 5000L},
        {56459055L, 3288380567368L, 165000L},
        {21084707L, 1589204008960L, 50000L},
        {24120521L, 1243764649177L, 20000L},
        {836877L, 212532333234L, 5293L},
        {55879741L, 13424854054078L, 250000L},
        {66388882L, 11300012790454L, 300000L},
        {94470955L, 7941038150919L, 2000L},
        {13613746L, 5012660712983L, 122L},
        {71852829L, 5262251868618L, 396L},
        {3857658L, 446109245044L, 20637L},
        {35491863L, 3887393269796L, 100L},
        {295632118L, 1265298439004L, 500000L},
        {49320113L, 1692106302503L, 123267L},
        {10966984L, 6222910652894L, 2018L},
        {41634280L, 2004508994767L, 865L},
        {10087714L, 6765558834714L, 1009L},
        {42270078L, 210360843525L, 200000L},
        {571091915L, 655011397250L, 2032520L},
        {51026781L, 1635726339365L, 37L},
        {61594L, 312318864132L, 500L},
        {11616684L, 5875978057357L, 20L},
        {60584529L, 1377717821301L, 78132L},
        {29818073L, 3033545989651L, 182L},
        {3855280L, 834647482043L, 16L},
        {58310711L, 1431562205655L, 200000L},
        {60226263L, 1386036785882L, 178226L},
        {3537634L, 965771433992L, 225L},
        {3760534L, 908700758784L, 328L},
        {80913L, 301864126445L, 4L},
        {3789271L, 901842209723L, 1L},
        {4051904L, 843419481286L, 1005L},
        {89141L, 282107742510L, 100L},
        {90170L, 282854635378L, 26L},
        {4229852L, 787503315944L, 137L},
        {4259884L, 781975090197L, 295L},
        {3627657L, 918682223700L, 34L},
        {813519L, 457546358759L, 173L},
        {89626L, 327856173057L, 27L},
        {97368L, 306386489550L, 50L},
        {93712L, 305866015731L, 4L},
        {3281260L, 723656594544L, 40L},
        {3442652L, 689908773685L, 18L},
    };

    for (long[] data : testData) {
      ExchangeProcessor processor = new ExchangeProcessor(supply, false);
      long anotherTokenQuant = processor.exchange(data[0], data[1], data[2]);
      processor = new ExchangeProcessor(supply, true);
      long result = processor.exchange(data[0], data[1], data[2]);
      long safeResult = SafeExchangeProcessor.INSTANCE.exchange(data[0], data[1], data[2]);
      Assert.assertNotEquals(anotherTokenQuant, result);
      Assert.assertEquals(safeResult, result);
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-98)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();

      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());

      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
      } else {
        anotherTokenID = firstTokenID;
      }

      long newBalance = subtractExact(accountCapsule.getBalance(), calcFee());
      accountCapsule.setBalance(newBalance);

      if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, tokenQuant));
      } else {
        accountCapsule.reduceAssetAmountV2(tokenID, tokenQuant, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(addExact(newBalance, anotherTokenQuant));
      } else {
        accountCapsule
            .addAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore);
      }

      accountStore.put(accountCapsule.createDbKey(), accountCapsule);

      Commons.putExchangeCapsule(exchangeCapsule, dynamicStore, exchangeStore, exchangeV2Store,
          assetIssueStore);

      ret.setExchangeReceivedAmount(anotherTokenQuant);
```
