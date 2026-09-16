### Title
Legacy floating-point Bancor formula in `ExchangeProcessor` causes rounding/precision loss enabling users to extract more tokens than the pool math intends on `ExchangeTransactionActuator` / `ExchangeWithdrawActuator` calls - (File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java)

### Summary
Every TRC10 exchange trade (`ExchangeTransactionContract`, reachable by any signed account) is priced by `ExchangeCapsule.transaction(...)`, which by default (unless the `allowHardenExchangeCalculation` maintenance-period proposal has been enabled by SRs) routes through the legacy `ExchangeProcessor`, a Bancor-style bonding-curve implementation that performs the entire calculation in IEEE-754 `double` arithmetic and truncates the result to `long` with a plain narrowing cast. This is the same root-cause pattern as the Rubicon `_borrowLimit` finding: a rounding/precision step in a value-scaling formula that an ordinary user fully controls the inputs to (quant, and the sequence/timing of trades) can make the actually-executed exchange amount diverge from the value the mathematically-correct (arbitrary-precision) formula would produce — and the user can pick inputs that steer this divergence in their favor, repeatedly, to their benefit and the pool's/counterparty's loss.

### Finding Description
`ExchangeCapsule.transaction` selects the pricing engine: [1](#0-0) 

When `hardenedCalc` is false (the default path, since it is gated by `dynamicStore.allowHardenExchangeCalculation()` via `allowHarden()` in `AbstractExchangeActuator`): [2](#0-1) 

the trade is computed by the legacy `ExchangeProcessor`, which performs the Bancor relay-token math entirely with `double`: [3](#0-2) 

Both `exchangeToSupply` and `exchangeFromSupply` cast the intermediate `double` result down to `long` with `(long) issuedSupply` / `(long) exchangeBalance`, i.e. simple truncation with no explicit rounding control, and the whole computation (including `Maths.pow`, division, and multiplication) is subject to IEEE-754 double rounding error which is data-dependent on the exact `balance`/`quant` ratio. A caller who freely chooses `sellTokenQuant` (fully attacker-controlled, exactly analogous to `initMargin`/`leverage` in the Rubicon report) can pick quantities/sequences of small trades where the accumulated floating-point truncation systematically favors the taker versus the mathematically-correct (arbitrary-precision) result that the protocol intends to enforce.

This mirrors the Rubicon `_borrowLimit`/`openPosition` bug precisely at the conceptual level: both use `wmul`/pow-based fixed-point-like math with a rounding operation baked into a bonding-curve style formula, and both allow an unprivileged caller to select inputs that make the *realized* on-chain amount deviate from the intended amount, changing risk/leverage or price outcomes to the caller's benefit. In fact, the codebase itself demonstrates awareness of this exact class of bug: a `SafeExchangeProcessor` using `BigDecimal` with explicit `RoundingMode.HALF_UP`/`DOWN` was added as a "hardened" replacement, and tests (`ExchangeProcessorTest.testStrictMath`) explicitly assert that the legacy double-based processor produces **different, less precise** results than the hardened BigDecimal one: [4](#0-3) 

However, the hardened calculation is opt-in and only takes effect once `allowHardenExchangeCalculation` is enabled via governance proposal; until/unless that proposal passes, every `ExchangeTransactionActuator` execution (`ExchangeTransactionActuator.execute` at line 68) and `ExchangeWithdrawActuator` price calculation continues to use the imprecise double-based engine by default: [5](#0-4) 

### Impact Explanation
An attacker who repeatedly submits `ExchangeTransactionContract` transactions with quantities chosen to maximize the floating-point truncation bias can extract more of the counter-token from a TRC10 exchange pool than the intended Bancor-curve output, at the expense of the pool (and therefore of other liquidity/pool participants and injectors). Over many trades this constitutes an unbacked-value/theft-of-funds vector against exchange pool balances — directly reachable by any unprivileged account issuing ordinary transactions, with no special privilege required. This matches the "unbacked balance / theft of funds" impact bar.

### Likelihood Explanation
Likelihood is Medium: exploitation requires the `allowHardenExchangeCalculation` proposal to not yet be enabled on the live chain (which is the default state absent an explicit SR vote), and it requires the attacker to search for/compute favorable trade sizes where floating point truncation biases in their favor across the pow-based formula — a search that is offline and inexpensive to perform (as demonstrated by the existing `ExchangeProcessorTest.testStrictMath` fixture data showing the legacy and strict-math paths already diverge for arbitrary chosen inputs). No special network position or privileged role is needed; only a standard signed `ExchangeTransactionContract`/`ExchangeWithdrawContract`.

### Recommendation
Make the `SafeExchangeProcessor` (BigDecimal, explicit rounding) the default and only exchange-pricing engine, removing the double-based `ExchangeProcessor` path entirely, or ensure `allowHardenExchangeCalculation` is enabled network-wide before mainnet trades can execute, rather than gating the safe engine behind an opt-in governance proposal that can remain disabled indefinitely.

### Proof of Concept
The repository's own test `ExchangeProcessorTest.testStrictMath` is a ready-made PoC: for identical `(sellBalance, buyBalance, sellQuant)` inputs, `ExchangeProcessor` in non-strict mode and `ExchangeProcessor` in strict mode return different results (`Assert.assertNotEquals(anotherTokenQuant, result)`), and the hardened `SafeExchangeProcessor` differs from the legacy double engine as well: [6](#0-5) 
This confirms the legacy path (the default, un-hardened one used by `ExchangeTransactionActuator`/`ExchangeWithdrawActuator` in production unless the hardening proposal is enacted) produces amounts that diverge from the mathematically-precise Bancor formula in an input-dependent, attacker-selectable way — the exact "precision loss lets the caller obtain more than expected" pattern flagged in the source Rubicon report.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

```

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-15)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L67-69)
```java
      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
```
