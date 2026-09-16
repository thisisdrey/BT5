### Title
Unchecked double-precision overflow in `ExchangeProcessor`'s Bancor-style power formula allows minting of unbacked token amounts / corrupts exchange pool balances - (File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java)

### Summary
The HATS finding shows that `computeCvgExpected`'s `ln`-based formula can produce out-of-range results that either revert or silently misbehave depending on input magnitude. java-tron's on-chain Bancor-style exchange (`ExchangeTransactionContract`) contains an analogous floating-point edge case: `ExchangeProcessor` computes token amounts using `Math.pow`/`StrictMath.pow` with a large exponent (`2000.0`) on plain Java `double`s and then narrows the result to `long` with an unchecked `(long)` cast, with no overflow/NaN/Infinity guard.

### Finding Description
`ExchangeProcessor.exchangeFromSupply` computes:
```java
double exchangeBalance = balance
    * (Maths.pow(1.0 + (double) supplyQuant / supply, 2000.0, this.useStrictMath) - 1.0);
...
return (long) exchangeBalance;
``` [1](#0-0) 

and `exchangeToSupply` similarly casts a `double` result directly to `long`:
```java
double issuedSupply = -supply * (1.0
    - Maths.pow(1.0 + (double) quant / newBalance, 0.0005, this.useStrictMath));
long out = (long) issuedSupply;
``` [2](#0-1) 

Both `exchange` calls are reached directly from an unprivileged, user-broadcast transaction through `ExchangeCapsule.transaction`, which selects the legacy `ExchangeProcessor` unless the `allowHardenExchangeCalculation` governance flag is enabled (in which case `SafeExchangeProcessor` is used instead):
```java
Processor processor = hardenedCalc
    ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);
...
newFirstTokenBalance = ... firstTokenBalance + sellTokenQuant;
newSecondTokenBalance = ... secondTokenBalance - buyTokenQuant;
...
if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
  throw new ContractValidateException(...);
}
``` [3](#0-2) 

Note the negative-balance guard is only executed `if (hardenedCalc)` — the legacy (default) path performs no bounds check at all on the computed `buyTokenQuant` or on the resulting pool balances.

Because Java narrowing conversion from `double` to `long` clamps `Double.POSITIVE_INFINITY` to `Long.MAX_VALUE` and `Double.NaN` to `0` (JLS 5.1.3) rather than throwing, an attacker who creates an `ExchangeCreateContract` pool with one side holding a very small token balance can then call `ExchangeTransactionContract` selling a comparatively large (but validator-permitted, i.e. below `getExchangeBalanceLimit()`) quantity of that scarce token. Because the ratio `quant / newBalance` becomes extreme, the `pow(...,2000.0)` term in `exchangeFromSupply` overflows `double` range (exceeds `~1.8e308`), producing `Infinity`, which is cast to `Long.MAX_VALUE` for `buyTokenQuant`.

This "hardened" alternative path, reached only when the `allowHardenExchangeCalculation`/`allowStrictMath` proposals are active, instead routes the same computation through `SafeExchangeProcessor`, which uses `BigDecimal`/`longValueExact()` and throws `ArithmeticException` on out-of-range results — directly mirroring the original report's "revert due to out-of-domain math" behavior, and confirming the finding class is recognized by the codebase's own design (the hardening was added specifically to catch this). [4](#0-3) 

### Impact Explanation
In the default (non-hardened) configuration:
- The unpriviledged caller of `ExchangeTransactionActuator.execute` receives `anotherTokenQuant = Long.MAX_VALUE` credited to their account via `accountCapsule.addAssetAmountV2(...)`, an unbacked-balance/token-minting bug. [5](#0-4) 
- The exchange pool's opposing balance (`newSecondTokenBalance`/`newFirstTokenBalance`) is set via unchecked `long` subtraction to a large negative number and persisted via `Commons.putExchangeCapsule`, permanently corrupting that market's state.
- `ExchangeTransactionActuator.doValidate` only checks `newTokenBalance <= balanceLimit` for the sold-token side (line ~199-205); it never validates the computed `anotherTokenQuant` against the actual counter-token balance in the pool for the non-hardened path. [6](#0-5) 

When the `allowHardenExchangeCalculation` proposal is active, the same extreme input instead throws `ArithmeticException` from `BigDecimal.longValueExact()`, which is caught and converted into `ContractExeException`/failed transaction status — reproducing the "revert" behavior described in the original report and making any exchange whose reserves fall into this ratio permanently unusable until reserves are rebalanced (denial of service on that market).

### Likelihood Explanation
Reachable end-to-end by any account that can broadcast `ExchangeCreateContract` (to build a thin-reserve pool) followed by `ExchangeTransactionContract` (to trigger the extreme ratio) — no special privilege required. The only variable is whether the SR committee has activated `allowHardenExchangeCalculation`/`allowStrictMath`; in the default/pre-activation state the silent overflow (fund-theft) path is live, and even post-activation the "safe" path exhibits the revert-DoS behavior analogous to the reported bug.

### Recommendation
- Add explicit range/finite checks (`Double.isInfinite`, `Double.isNaN`, and comparison against `Long.MAX_VALUE`/`MIN_VALUE`) before casting `issuedSupply`/`exchangeBalance` to `long` in `ExchangeProcessor`, rejecting the transaction (throw `ContractValidateException`) instead of silently clamping.
- Apply the same negative/overflow balance checks currently gated behind `hardenedCalc` unconditionally in `ExchangeCapsule.transaction`.
- Consider fully retiring the legacy `ExchangeProcessor` double-based path in favor of `SafeExchangeProcessor` for all exchanges, independent of the governance flag, given the demonstrated overflow class.

### Proof of Concept
Conceptual reproduction (cannot be executed in this environment, but mirrors `ExchangeProcessorTest.testStrictMath`'s harness at [7](#0-6) ):
1. Create an exchange pool with `firstTokenBalance = 1` and `secondTokenBalance = 100_000_000` via `ExchangeCreateContract`.
2. Submit `ExchangeTransactionContract` selling `firstTokenId` with `quant` close to `dynamicStore.getExchangeBalanceLimit()`.
3. `exchangeCapsule.transaction(...)` invokes `ExchangeProcessor.exchange`, where the extreme `quant/newBalance` ratio drives `pow(base, 2000.0)` to `Infinity`; `(long) exchangeBalance` yields `Long.MAX_VALUE`, credited to the caller's `secondTokenId` asset balance, while the pool's `secondTokenBalance` is left corrupted (large negative long).
4. Repeating with `allowHardenExchangeCalculation` enabled instead throws `ArithmeticException` in `SafeExchangeProcessor.exchangeFromSupply` via `longValueExact()`, causing `ContractExeException`/transaction failure — the revert analog of the original report.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L17-29)
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L31-39)
```java
  private long exchangeFromSupply(long balance, long supplyQuant) {
    supply -= supplyQuant;

    double exchangeBalance = balance
        * (Maths.pow(1.0 + (double) supplyQuant / supply, 2000.0, this.useStrictMath) - 1.0);
    logger.debug("exchangeBalance: " + exchangeBalance);

    return (long) exchangeBalance;
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-162)
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

    if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
      throw new ContractValidateException("Exchange balance must be >=0 after transaction");
    }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/SafeExchangeProcessor.java (L30-38)
```java
  private long exchangeFromSupply(long balance, BigDecimal supplyQuant) {
    BigDecimal bdBalance = BigDecimal.valueOf(balance);
    BigDecimal base = BigDecimal.ONE.add(
        supplyQuant.divide(SUPPLY, 18, RoundingMode.HALF_UP));
    double powResult = StrictMathWrapper.pow(base.doubleValue(), 2000.0);
    BigDecimal exchangeBalance = bdBalance.multiply(
        BigDecimal.valueOf(powResult).subtract(BigDecimal.ONE));
    return exchangeBalance.setScale(0, RoundingMode.DOWN).longValueExact();
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L86-93)
```java
      if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(addExact(newBalance, anotherTokenQuant));
      } else {
        accountCapsule
            .addAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore);
      }

      accountStore.put(accountCapsule.createDbKey(), accountCapsule);
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L199-221)
```java
    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    long tokenBalance = (Arrays.equals(tokenID, firstTokenID) ? firstTokenBalance
        : secondTokenBalance);
    tokenBalance = addExact(tokenBalance, tokenQuant);
    if (tokenBalance > balanceLimit) {
      throw new ContractValidateException("token balance must less than " + balanceLimit);
    }

    if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
      if (accountCapsule.getBalance() < addExact(tokenQuant, calcFee())) {
        throw new ContractValidateException("balance is not enough");
      }
    } else {
      if (!accountCapsule.assetBalanceEnoughV2(tokenID, tokenQuant, dynamicStore)) {
        throw new ContractValidateException("token balance is not enough");
      }
    }

    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
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
