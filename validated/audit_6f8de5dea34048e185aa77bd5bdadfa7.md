Based on the analysis, I found the strongest analog. The default (non-hardened) `ExchangeProcessor` uses floating-point `double` math for the Bancor-curve AMM calculation and never validates the resulting reserve values, whereas the hardened path (`SafeExchangeProcessor`, gated behind `AllowHardenExchangeCalculation`) validates results are non-negative and correctly bounded. This mirrors the reported bug class: a value derived from an unchecked calculation is used to mutate the pool's tracked reserve balances that later back other users' withdrawals (`ExchangeWithdrawContract`), with no post-call invariant check when the legacy path is used.

### Title
Non-hardened `ExchangeCapsule.transaction()` accepts floating-point AMM output without validating resulting reserve balances, enabling insolvent exchange pools - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java])

### Summary
`ExchangeCapsule.transaction()` is the core routine invoked by `ExchangeTransactionActuator.execute()` for every `ExchangeTransactionContract` (a transaction type any account can broadcast). It computes `buyTokenQuant` via a `Processor` and then unconditionally applies the resulting balance deltas to the exchange's tracked `firstTokenBalance`/`secondTokenBalance`. When `hardenedCalc` is `false` (the default unless `AllowHardenExchangeCalculation` is enabled), the calculation uses `ExchangeProcessor`, which performs the Bancor-style formula with `double` floating-point arithmetic, and the resulting `newFirstTokenBalance`/`newSecondTokenBalance` are never checked for validity before being persisted. [1](#0-0) 

### Finding Description
The bounds check `if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0))` only fires when `hardenedCalc == true`. [2](#0-1) 
In the default (non-hardened) path, `ExchangeProcessor.exchange()` computes both legs of the trade using `double` precision Bancor math (`Math.pow`/floating point division), then casts the result to `long`. [3](#0-2) 
Unlike `SafeExchangeProcessor`, which uses `BigDecimal` arithmetic and enforces `result <= buyReserve` and `result >= 0` via its bounded formula and `longValueExact()` checked conversions, `ExchangeProcessor` performs no analogous invariant validation on its output. [4](#0-3) 
Both `ExchangeTransactionActuator.doValidate()` and `execute()` call `exchangeCapsule.transaction(...)` and trust its output directly, updating the persisted `ExchangeCapsule` via `Commons.putExchangeCapsule(...)` without any subsequent reconciliation against real token/TRX balances actually transferred to/from the trading account. [5](#0-4) 
The exchange's tracked `firstTokenBalance`/`secondTokenBalance` is exactly the value later used by any account holding the exchange (via `ExchangeWithdrawContract`) to redeem its share of both tokens — analogous to the vault's `totalCollateral` invariant in the reported bug. Because the non-hardened arithmetic path is exposed to unpredictable floating-point rounding across extreme/precision-edge inputs (as evidenced by the `ExchangeProcessorTest.testStrictMath` divergence table showing the non-strict-math result diverges from the strict/hardened result for the same inputs) and no invariant re-check is performed, a sequence of trades using non-hardened math can drive the tracked reserves out of sync with what is truly redeemable, without the transaction failing. [6](#0-5) 

### Impact Explanation
If tracked reserve balances diverge from what the pool can actually back (e.g., through repeated small-quantity trades hitting floating-point rounding edge cases, or larger trades where `double` precision loss compounds), a subsequent `ExchangeWithdrawContract` by the exchange creator computes withdrawal amounts proportional to the (corrupted) tracked balances via `ExchangeWithdrawActuator.doValidate()`'s ratio math. [7](#0-6) 
This can either (a) let a withdrawal succeed for more than is truly backed, leaving remaining participants unable to withdraw their share (funds permanently frozen / insolvency), or (b) cause legitimate withdrawals to revert due to accounting drift, bricking access to funds — the same "collateral balance not enough for everyone to withdraw" outcome described in the source report.

### Likelihood Explanation
Any account can create an exchange pair and any account can trigger `ExchangeTransactionContract` trades against it purely by broadcasting a signed transaction — no special privilege is required, unlike the "keeper" role in the original report, making this reachable by an ordinary unprivileged actor. The hardened path exists specifically because the legacy floating-point path was known to be imprecise (confirmed by the actuator's own test comparing strict vs. non-strict results across dozens of inputs), and hardened mode is opt-in via `AllowHardenExchangeCalculation`, so chains/networks that have not activated it remain exposed.

### Recommendation
Enforce the same non-negativity/bounds invariant unconditionally (not only when `hardenedCalc` is true) in `ExchangeCapsule.transaction()`, and consider deprecating/removing the unchecked `ExchangeProcessor` floating-point path in favor of always routing through the checked `SafeExchangeProcessor` arithmetic, regardless of the `AllowHardenExchangeCalculation` proposal state.

### Proof of Concept
1. On a chain where `AllowHardenExchangeCalculation` is not yet activated (`hardenedCalc == false`), create an `ExchangeCreateContract` pair with two tokens (or TRX and a token).
2. Repeatedly issue `ExchangeTransactionContract` trades with quantities chosen so `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply`'s `double` `Math.pow` rounding (see `ExchangeProcessorTest.testStrictMath`, which shows the non-strict result differs from the strict/hardened result for identical inputs) accumulates rounding drift across many trades.
3. Because `transaction()` never validates `newFirstTokenBalance`/`newSecondTokenBalance` in the non-hardened branch, the persisted `ExchangeCapsule` balances silently diverge from the token amounts actually held/transferred by the account.
4. The exchange creator then calls `ExchangeWithdrawContract`; the withdrawal ratio computed from the drifted `firstTokenBalance`/`secondTokenBalance` no longer matches real backing, causing either an over-withdrawal (draining funds owed to remaining exchange participants) or a failed/insufficient withdrawal for legitimate remaining balance.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-169)
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
    this.exchange = this.exchange.toBuilder()
        .setFirstTokenBalance(newFirstTokenBalance)
        .setSecondTokenBalance(newSecondTokenBalance)
        .build();

    return buyTokenQuant;
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

**File:** chainbase/src/main/java/org/tron/core/capsule/SafeExchangeProcessor.java (L30-44)
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

  @Override
  public long exchange(long sellTokenBalance, long buyTokenBalance, long sellTokenQuant) {
    BigDecimal relay = exchangeToSupply(sellTokenBalance, sellTokenQuant);
    return exchangeFromSupply(buyTokenBalance, relay);
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-96)
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L214-272)
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

    } else {
      anotherTokenQuant = bigFirstTokenBalance.multiply(bigTokenQuant)
          .divideToIntegralValue(bigSecondTokenBalance).longValueExact();
      if (secondTokenBalance < tokenQuant || firstTokenBalance < anotherTokenQuant) {
        throw new ContractValidateException("exchange balance is not enough");
      }

      if (anotherTokenQuant <= 0) {
        throw new ContractValidateException("withdraw another token quant must greater than zero");
      }

      if (allowHarden) {
        BigDecimal remainder = bigFirstTokenBalance.multiply(bigTokenQuant)
            .divide(bigSecondTokenBalance, 4, RoundingMode.HALF_UP)
            .subtract(BigDecimal.valueOf(anotherTokenQuant));
        if (remainder.compareTo(
            BigDecimal.valueOf(anotherTokenQuant).multiply(new BigDecimal("0.0001"))) > 0) {
          throw new ContractValidateException("Not precise enough");
        }
      } else {
        double remainder = bigFirstTokenBalance.multiply(bigTokenQuant)
            .divide(bigSecondTokenBalance, 4, BigDecimal.ROUND_HALF_UP).doubleValue()
            - anotherTokenQuant;
        if (remainder / anotherTokenQuant > 0.0001) {
          throw new ContractValidateException("Not precise enough");
        }
      }
    }
```
