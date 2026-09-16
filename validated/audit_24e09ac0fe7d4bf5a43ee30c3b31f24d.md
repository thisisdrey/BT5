## Analog Found

### Title
Loss of accuracy in AMM-style Exchange (Bancor) fee/output calculation using floating-point math instead of exact integer arithmetic - (File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java)

### Summary
The external report describes a mismatch between a fee-adjusted, high-precision value (`amountInPostFee`, scaled by 10000) and a raw reserve delta computed with regular subtraction, causing a loss of accuracy in an AMM output computation. The java-tron `TRXExchange`/`ExchangeTransactionContract` bonding-curve swap implementation has an analogous defect: `ExchangeProcessor` computes the AMM swap output using `double`/`Math.pow` floating-point arithmetic and truncates to `long` via a plain cast, rather than using exact integer/BigDecimal math throughout — exactly the class of "loose of accuracy" bug cited in the report.

### Finding Description
`ExchangeCapsule.transaction()` selects the swap math engine based on `hardenedCalc`/`useStrictMath` flags: [1](#0-0) . When the exchange's hardened calculation proposal is not enabled, the default `ExchangeProcessor` is used, which performs the entire bonding-curve conversion (both `exchangeToSupply` and `exchangeFromSupply`) in `double` precision and truncates the result with a bare `(long)` cast: [2](#0-1) . This is the direct analog of the reported bug: a fee/ratio-adjusted quantity computed via `Math.pow` (floating point, akin to the report's "powered by 10000" scaled value) is then combined with plain arithmetic truncation (`(long)` cast), producing accumulated rounding error rather than exact accounting. The project's own test suite explicitly documents that the non-hardened, double-based path produces different (i.e., inaccurate) results compared to the exact BigDecimal-based `SafeExchangeProcessor`: [3](#0-2) .

Whether this legacy path is exploitable in production depends on the `allowHardenExchangeCalculation` dynamic property gating in `AbstractExchangeActuator.allowHarden()`: [4](#0-3) , and on `dynamicStore.allowStrictMath()` used by `ExchangeTransactionActuator`: [5](#0-4) . I was not able to confirm from the index whether these dynamic properties default to disabled (legacy double-math path active) or enabled (hardened BigDecimal path active) on mainnet — this requires checking the on-chain proposal state / default values in `DynamicPropertiesStore`, which the index did not fully resolve.

### Impact Explanation
If the hardened path is not the active default, any unprivileged account can call `ExchangeTransactionContract` (via `ExchangeTransactionActuator`) to repeatedly swap through the TRX/TRC10 bonding-curve exchange. Because the swap math truncates floating-point results non-deterministically relative to exact integer math, an attacker could systematically bias trades (e.g., via small repeated swaps) to extract more value from the pool than the exact AMM curve allows, or cause the exchange's internal balances to drift from the true invariant over many transactions — a form of unbacked-value extraction from the liquidity pool, which is the "theft/unbacked balance" impact class allowed by this scan's scope.

### Likelihood Explanation
Likelihood is only moderate-to-uncertain because: (1) the actuator is reachable by any signed transaction with no special privilege (`ExchangeTransactionContract` — order/exchange interaction path in scope), and (2) the vulnerable arithmetic path (`ExchangeProcessor`, non-hardened) is present and reachable in code, but (3) whether it is actually the *active* default in the current chain configuration is unverified — the codebase clearly shows this was already identified as a systemic issue and a parallel hardened implementation (`SafeExchangeProcessor`, gated by `allowHardenExchangeCalculation`) exists specifically to fix it, suggesting the maintainers are aware and may already have activated the fix via governance proposal on mainnet.

### Recommendation
Confirm and enforce that `allowHardenExchangeCalculation` (and `allowStrictMath`) are permanently enabled (hard-forked in, not merely a togglable proposal) so that all exchange swap calculations exclusively use the exact `SafeExchangeProcessor` (BigDecimal-based) path in `ExchangeCapsule.transaction()` for every `ExchangeTransactionActuator`, `ExchangeInjectActuator`, and `ExchangeWithdrawActuator` invocation, removing the legacy floating-point `ExchangeProcessor` code path from production use entirely to eliminate any residual "loss of accuracy" attack surface.

### Proof of Concept
Not independently reproducible from static analysis alone: exploitation requires knowing the live value of the `allowHardenExchangeCalculation` dynamic property on the target network (mainnet/testnet), which is not available via the code index. The existing regression test demonstrates the underlying precision discrepancy between the legacy and hardened processors, which would form the basis of a PoC once the live gating state is confirmed: [6](#0-5) .

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

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-15)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L66-69)
```java

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
```
