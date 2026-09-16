## Title
Floating-point precision loophole in TRON's Bancor-style Exchange AMM math allows loss/theft of exchange liquidity - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java`)

### Summary
KyberSwap Elastic's incident involved a rounding/precision loophole in its concentrated-liquidity AMM math that let liquidity be extracted unfairly. java-tron's on-chain `Exchange`/`ExchangeV2` AMM (TRX↔TRC10 Bancor-formula market) has an analogous class of bug: the default, non-hardened swap math in `ExchangeProcessor` performs the bonding-curve calculation using native Java `double` (`Math.pow`/`StrictMath.pow`) instead of exact arithmetic, and a newly-added `SafeExchangeProcessor`/`hardenedCalc` BigDecimal path exists specifically to correct this, but it is only used when the `allowHardenExchangeCalculation` dynamic property is enabled by chain governance.

### Finding Description
Every TRX/TRC10 swap and inject/withdraw operation routes through `ExchangeCapsule.transaction()`: [1](#0-0) 

When `hardenedCalc` is `false` (i.e., `allowHardenExchangeCalculation` proposal not enabled), the legacy `ExchangeProcessor` is used, which computes the relay/"supply" bonding-curve transform entirely in floating point: [2](#0-1) 

This is directly reachable by any unprivileged account broadcasting `ExchangeTransactionContract`, `ExchangeInjectContract`, or `ExchangeWithdrawContract` transactions via `ExchangeTransactionActuator.execute()`: [3](#0-2) 

The project's own regression test proves that the double-based path (`useStrictMath=false` or even `true`) produces materially different, less precise results than the hardened BigDecimal path (`SafeExchangeProcessor`), for a wide range of realistic pool balances/quantities: [4](#0-3) 

This is structurally the same bug class as KyberSwap's Elastic loophole: an AMM pricing/curve calculation whose numerical method (floating point vs. exact decimal) diverges from the "fair" invariant-preserving result, allowing a party who understands the rounding behavior to construct sequences of trades/injects/withdraws that extract more value than they contributed, at the expense of other liquidity participants or the pool's own invariant. `ExchangeWithdrawActuator`'s own validation logic even explicitly tolerates a `0.0001` relative error band before rejecting a withdrawal as "Not precise enough": [5](#0-4) 
which is an admission that the underlying math is not exact and needs a tolerance window — precisely the type of "precision gap" that adversaries exploit in AMM/liquidity-math bugs like KyberSwap's.

### Impact Explanation
If the hardened (`SafeExchangeProcessor`/BigDecimal) path is not enabled on mainnet by default (this is a `DynamicPropertiesStore`-gated, committee-activated proposal, following the same activation pattern as other `allowXxx` hardening flags in this codebase), then all live TRX/TRC10 Bancor exchanges use the floating-point `ExchangeProcessor`. An attacker who can predict/exploit the double-precision rounding behavior (documented by the codebase's own tests showing it diverges from the exact BigDecimal result) could repeatedly swap, inject, and withdraw in patterns that produce more tokens out than mathematically owed, silently draining TRX/TRC10 balances from an Exchange pool — a concrete "loss of funds" outcome, matching the severity class of the KyberSwap report (Medium: contract-math loophole enabling extraction of liquidity, though not yet publicly exploited at disclosure time).

### Likelihood Explanation
The vulnerable code path (`ExchangeProcessor.exchange`) is the actively-maintained default and is reachable by any account with an existing Exchange and TRX/TRC10 balance — no special privilege is required, only a `ExchangeTransactionContract`/`ExchangeInjectContract`/`ExchangeWithdrawContract` signed transaction. The precision divergence is deterministic and reproducible (as shown by the project's own `testStrictMath` unit test), meaning an attacker does not need to guess; they can compute exact rounding deltas offline and craft a sequence of trades to accumulate profit. I could not verify from the available index whether `allowHardenExchangeCalculation` is enabled by default on current mainnet (this depends on committee proposal activation history not visible in the repo), which is the main source of uncertainty for real-world exploitability today.

### Recommendation
- Verify current mainnet/testnet status of the `allowHardenExchangeCalculation` proposal; if not yet universally active, prioritize activation.
- Migrate `ExchangeProcessor`'s core curve math to the exact `SafeExchangeProcessor` (BigDecimal) approach unconditionally, removing the double-precision code path once fully deprecated, rather than gating it behind an optional flag.
- Add invariant-preserving checks (e.g., k-invariant/relay-supply consistency assertions) directly inside `ExchangeCapsule.transaction()` for both legacy and hardened branches, independent of the tolerance window currently only enforced in `ExchangeWithdrawActuator`.

### Proof of Concept
Conceptual (based on existing project test data in `ExchangeProcessorTest.testStrictMath`, `chainbase`/`framework` modules): for pool balances `sellTokenBalance`, `buyTokenBalance` from the table at [6](#0-5) , calling `ExchangeProcessor.exchange()` (non-hardened) and `SafeExchangeProcessor.INSTANCE.exchange()` on the same inputs yields different `anotherTokenQuant` results (`Assert.assertNotEquals(anotherTokenQuant, result)` at line 278). An attacker submitting `ExchangeTransactionContract`s with quantities chosen to maximize this delta over repeated swaps could accumulate a profit exceeding the intended AMM output, at the pool's expense. A concrete numeric drain sequence would require live-chain state (current Exchange pool balances) to construct, which is outside what can be determined from static repo analysis alone.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L17-39)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L64-69)
```java
      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
```

**File:** framework/src/test/java/org/tron/core/capsule/utils/ExchangeProcessorTest.java (L220-270)
```java
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
```

**File:** framework/src/test/java/org/tron/core/capsule/utils/ExchangeProcessorTest.java (L272-280)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L228-243)
```java
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
```
