### Title
Non-hardened Bancor-formula floating-point math in `ExchangeProcessor.exchange()` allows precision-manipulation profit extraction from TRC10 Exchange pools - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java`)

### Summary
The external report describes an AMM whose reserve/price accounting can be manipulated (via a non-swap-path reserve update) and then used by a pricing function to extract value at a distorted rate. The closest reachable analog in java-tron is the TRC10 "Exchange" bancor-relay pricing engine: `ExchangeTransactionActuator` lets any funded account issue an `ExchangeTransactionContract` that calls `ExchangeCapsule.transaction()`, which by default routes through `ExchangeProcessor`, a **double-precision floating point** implementation of the bancor curve (`Math.pow`), rather than exact/rational arithmetic.

### Finding Description
`ExchangeCapsule.transaction()` picks the processor used to price a trade: [1](#0-0) 

When `hardenedCalc` is false (the legacy/default code path unless the `allowHardenExchangeCalculation` proposal is separately activated on-chain), it uses `ExchangeProcessor`, which computes the bancor relay/withdraw amounts with `double` arithmetic and `Math.pow`: [2](#0-1) 

A later `SafeExchangeProcessor` was introduced that performs the same math with `BigDecimal`/exact arithmetic and explicit overflow checks, gated by a separate `allowHarden()`/`allowHardenExchangeCalculation` flag: [3](#0-2) 

The project's own tests demonstrate that, for identical inputs, the floating-point (`useStrictMath=false`/legacy) processor and the exact (`SafeExchangeProcessor`/strict) processor produce **different results** across dozens of realistic reserve/quant combinations: [4](#0-3) 

This is the same bug class as the external report: a pricing/reserve-accounting routine whose output can diverge from the "true" proportional value due to non-exact math, which an attacker can exploit through carefully chosen sequences of trades (buy/sell in opposite directions, exploiting the asymmetric rounding of `exchangeToSupply`/`exchangeFromSupply`) to extract more value from the pool than deposited, or to make the pool insolvent relative to its nominal reserves — conceptually equivalent to how the EtnProduct attacker exploited a pool's price/reserve computation to buy assets far below their backed value. The actuator itself performs no independent sanity check on the processor's output (e.g., no check that value extracted does not exceed a conserved invariant); it simply accepts `anotherTokenQuant` from `exchangeCapsule.transaction(...)` and moves balances accordingly: [5](#0-4) 

### Impact Explanation
If floating-point drift can be steered in the attacker's favor via a chosen sequence of `ExchangeTransactionContract` calls (a broadcaster-reachable, unprivileged action — no special permissions needed, only sufficient TRX/TRC10 balance and the `expected` slippage field, which the attacker fully controls), the attacker can repeatedly extract more of the counter-asset than the bancor invariant would allow, draining the exchange's reserves at other participants' expense — i.e., theft of funds / creation of an unbacked balance within the TRC10 Exchange feature. Because this affects on-chain state (`ExchangeCapsule` balances and account asset/TRX balances) via a normal actuator, this is deterministic across all full nodes and does not itself cause a chain split, but it directly enables value extraction beyond what backing reserves justify.

### Likelihood Explanation
The path is reachable by any account (creator not required, unlike Inject/Withdraw which require the creator address) via a single signed `ExchangeTransactionContract`, using only the public, non-privileged `ExchangeTransactionActuator`. The mere existence of `SafeExchangeProcessor` as an opt-in replacement (gated behind an `allowHardenExchangeCalculation` proposal) strongly suggests the double-based `ExchangeProcessor` path was recognized internally as imprecise/exploitable and is still the default unless that specific committee proposal has been activated on a given chain — meaning any TRON-based chain that has not enabled `allowHardenExchangeCalculation` remains on the exploitable floating-point path. Actually weaponizing this into a net-positive-profit sequence of trades requires numeric analysis of `Maths.pow` rounding behavior (I was not able to fully derive or verify a concrete profitable trade sequence within the scope of this investigation), so likelihood should be treated as **plausible but unconfirmed** pending further arithmetic/fuzzing analysis.

### Recommendation
- Require `allowHardenExchangeCalculation` (i.e., mandate `SafeExchangeProcessor`) unconditionally for `ExchangeTransactionActuator`, `ExchangeInjectActuator`, and `ExchangeWithdrawActuator`, rather than leaving it as an optional, proposal-gated code path.
- Add an explicit invariant check in `ExchangeCapsule.transaction()` (independent of which processor computed the result) verifying that `firstTokenBalance * secondTokenBalance` (the constant-product/bancor invariant proxy) never decreases beyond the fee/slippage tolerance after a trade, rejecting the transaction otherwise.
- Deprecate and remove `ExchangeProcessor`'s double/`Math.pow`-based implementation from any code path reachable without the hardening flag.

### Proof of Concept
A concrete, exploit-grade PoC (a chosen sequence of `ExchangeTransactionContract` trades that provably yields net profit purely from floating-point rounding divergence) could not be constructed with confidence from static code review alone; `ExchangeProcessorTest.testStrictMath` at [4](#0-3)  only proves that outputs *differ* between the strict and non-strict paths for the same inputs — it does not by itself demonstrate a round-trip profitable arbitrage. Confirming actual exploitability would require numerical/fuzzing analysis of `Maths.pow` (in `common/src/main/java/org/tron/common/math/Maths.java`) against `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` to find reserve/quant combinations where a buy-then-sell round trip returns more than was paid in.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

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

**File:** framework/src/test/java/org/tron/core/capsule/utils/ExchangeProcessorTest.java (L218-281)
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
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L64-91)
```java
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
```
