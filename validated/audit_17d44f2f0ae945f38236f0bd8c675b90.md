## Title
Unbounded bonding‑curve exchange arithmetic can drive `ExchangeCapsule` reserves negative when hardened calculation is disabled - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java`)

### Summary
The reported class of bug — a ratio/curve computation whose result is not bounded against the actual available reserve, allowing the derived amount to exceed real backing — has a direct analog in java-tron's on-chain **Bancor-style asset exchange**. `ExchangeCapsule.transaction()` computes the amount to be paid out (`buyTokenQuant`) using floating-point, double-precision bonding-curve math in `ExchangeProcessor` and only checks `newFirstTokenBalance < 0 || newSecondTokenBalance < 0` when the `hardenedCalc` flag is `true`. When that flag is `false` (the legacy/original code path, gated by `DynamicPropertiesStore.allowHardenExchangeCalculation()`), the resulting reserve balance can go negative with no validation, mirroring the "`reserves > cash`" scenario from the report where an unguarded subtraction from a finite pool is allowed to underflow the conceptual invariant.

### Finding Description
`ExchangeCapsule.transaction()` computes the traded quantity via a `Processor`: [1](#0-0) 

Only the `hardenedCalc` branch performs a sanity check: [2](#0-1) 

The legacy (`hardenedCalc == false`) `ExchangeProcessor.exchange()` implementation relies purely on `double` arithmetic and `Math.pow`: [3](#0-2) 

This is precisely the class of bug described in the external report: an internal ratio/curve value (bounded conceptually between 0 and the pool reserve) is computed without a hard invariant check, and floating-point/precision effects (analogous to "reserves growing past cash automatically") can let the computed payout quantity exceed the real reserve balance, producing a negative reserve. The report's own recommendation — clamp/validate rather than trust the raw formula — is exactly what the `hardenedCalc` path was later added to do (`SafeExchangeProcessor`, which uses `BigDecimal` and explicit `addExact`/`subtractExact`): [4](#0-3) 

The actuators reachable by an ordinary signed transaction (`ExchangeTransactionActuator`, `ExchangeInjectActuator`, `ExchangeWithdrawActuator`) invoke `exchangeCapsule.transaction(...)` passing `allowHarden()`, which reads `chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation()`: [5](#0-4) [6](#0-5) 

Whenever this dynamic property is `0` (which is the historical/legacy default before the hardening feature was activated via committee proposal — the exact current activation state on a given network could not be verified from the index), the non-hardened `ExchangeProcessor` path is used and no negative-balance check is performed before the pool balances are persisted via `Commons.putExchangeCapsule`.

### Impact Explanation
If the double-precision bonding-curve math yields a `buyTokenQuant` (or `anotherTokenQuant` for inject/withdraw) larger than the actual counter-token reserve, `ExchangeCapsule.transaction()` will silently write a negative `firstTokenBalance`/`secondTokenBalance` into the `ExchangeStore`/`ExchangeV2Store`. This is an unbacked-balance condition analogous to `reserves > cash`: it lets a trader extract more of the counter-asset than the pool actually holds, and any subsequent trade against the corrupted pool state (whose "reserve" is now negative) further amplifies the miscalculation, creating additional unbacked value or driving the exchange into a permanently inconsistent state — a concrete theft-of-funds / unbacked-balance class of impact within the constraints of this review.

### Likelihood Explanation
Reachability requires only a single signed `ExchangeTransactionContract`, `ExchangeInjectContract`, or `ExchangeWithdrawContract` from any account address (no special privileges) targeting an existing exchange pair. The actual likelihood of triggering an overshoot depends on (a) the specific pool balances/quantities chosen (an attacker can search for edge-case reserve ratios and quantities where the floating point `Math.pow` computation rounds up past the true bonding-curve output) and (b) whether `allowHardenExchangeCalculation` is currently active on the target network — a Committee-controlled activation flag whose live value could not be confirmed from the indexed code alone.

### Recommendation
- Make the hardened, invariant-checked `SafeExchangeProcessor` path (`chainbase/src/main/java/org/tron/core/capsule/SafeExchangeProcessor.java`) unconditional in `ExchangeCapsule.transaction()`, i.e., remove/deprecate the legacy `ExchangeProcessor` double-math branch entirely instead of gating it behind a proposal flag.
- Independent of which processor is used, always assert `newFirstTokenBalance >= 0 && newSecondTokenBalance >= 0` before persisting the updated `ExchangeCapsule`, and reject the transaction (throw `ContractValidateException`/`ContractExeException`) if the invariant is violated — mirroring the report's recommendation to clamp/guard the ratio computation rather than trust it unconditionally.
- Audit all other unguarded floating-point ratio computations reachable from a single transaction (e.g., `MortgageService.computeReward`, `IncentiveManager.reward`, `Wallet.calcCanDelegatedBandWidthMaxSize`) for similar "assume-invariant-then-compute" patterns; several of these already mix `double` division with unguarded assumptions (e.g., `assert totalEnergyWeight > 0` in `RepositoryImpl.calculateGlobalEnergyLimit`, which is a no-op in production since Java assertions are disabled by default), though these were not found to be independently exploitable to the same severity as the exchange path above.

### Proof of Concept
1. Create an exchange pair via `ExchangeCreateActuator` with a first/second token balance ratio chosen to sit near a rounding boundary of the `Math.pow(1 + quant/newBalance, 0.0005)` / `Math.pow(1 + supplyQuant/supply, 2000.0)` computations in `ExchangeProcessor` (values demonstrating divergence between `ExchangeProcessor` and `SafeExchangeProcessor` are already present in the repo's own test data, e.g. `ExchangeProcessorTest.testStrictMath`, which explicitly asserts `Assert.assertNotEquals(anotherTokenQuant, result)` between the double-based and BigDecimal-based implementations for many input triples): [7](#0-6) 
2. Send an `ExchangeTransactionContract` sized so that, on the pool whose `allowHardenExchangeCalculation` proposal is inactive, the legacy `ExchangeProcessor.exchange()` computes a `buyTokenQuant` marginally larger than the true reserve output that `SafeExchangeProcessor` would compute.
3. Because `ExchangeCapsule.transaction()` skips the `newFirstTokenBalance < 0 || newSecondTokenBalance < 0` check when `hardenedCalc == false`, the transaction succeeds and `Commons.putExchangeCapsule` persists a negative or under-collateralized reserve, letting the caller receive counter-tokens the pool does not actually hold.

**Caveat:** I was not able to confirm from the indexed code the current live/default value of the `allowHardenExchangeCalculation` committee proposal on any specific deployed java-tron network, nor could I fully trace whether every historical release ships with this proposal already activated by default — this would need to be verified directly against a running node's `DynamicPropertiesStore` state (a full Devin session with repository/config access would be needed to confirm this precisely).

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

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L160-167)
```java
    if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
      throw new ContractValidateException("Exchange balance must be >=0 after transaction");
    }
    this.exchange = this.exchange.toBuilder()
        .setFirstTokenBalance(newFirstTokenBalance)
        .setSecondTokenBalance(newSecondTokenBalance)
        .build();

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

**File:** chainbase/src/main/java/org/tron/core/capsule/SafeExchangeProcessor.java (L1-45)
```java
package org.tron.core.capsule;

import java.math.BigDecimal;
import java.math.RoundingMode;
import lombok.extern.slf4j.Slf4j;
import org.tron.common.math.StrictMathWrapper;

@Slf4j(topic = "capsule")
public class SafeExchangeProcessor implements ExchangeCapsule.Processor {

  private static final BigDecimal SUPPLY = BigDecimal.valueOf(1_000_000_000_000_000_000L);

  public static final SafeExchangeProcessor INSTANCE = new SafeExchangeProcessor();

  private SafeExchangeProcessor() {

  }

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
}
```

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-15)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
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
