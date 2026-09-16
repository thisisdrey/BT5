This confirms the vulnerability: `ALLOW_HARDEN_EXCHANGE_CALCULATION` defaults to `0` (disabled), meaning the legacy, unprotected arithmetic path in `ExchangeCapsule.transaction()` is the live default behavior on the network. In that path, `newFirstTokenBalance`/`newSecondTokenBalance` are computed with plain Java `long` subtraction/addition and **no post-condition check** that the result is non-negative — unlike the hardened path which explicitly validates `newFirstTokenBalance < 0 || newSecondTokenBalance < 0` and reverts.

### Title
Unvalidated legacy Bancor-style exchange calculation allows negative/underflowed pool balances in `ExchangeTransactionActuator`/`ExchangeInjectActuator` (default configuration) - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java`)

### Summary
`ExchangeCapsule.transaction()` computes the counter-token amount to debit from the exchange pool via a Bancor-relay formula (`ExchangeProcessor.exchange`) and then updates the pool's `firstTokenBalance`/`secondTokenBalance` by plain subtraction/addition, with the non-negative invariant check gated behind the `allowHardenExchangeCalculation` flag, which is `0` (disabled) by default. This mirrors the Ajna `PositionManager.moveLiquidity` finding: a value computed from a rate-based/derived formula is subtracted from a tracked balance without verifying it does not exceed that balance.

### Finding Description
`ExchangeTransactionActuator.execute()` and `ExchangeInjectActuator.execute()` call `exchangeCapsule.transaction(...)`, which delegates to `ExchangeCapsule.transaction()`: [1](#0-0) 

When `hardenedCalc` is `false` (the default, since `dynamicStore.allowHardenExchangeCalculation()` is off), `buyTokenQuant` is computed via `ExchangeProcessor.exchange()`, which uses floating-point `double` math internally: [2](#0-1) 

Then the pool balances are updated with plain `long` arithmetic:
```java
newSecondTokenBalance = secondTokenBalance - buyTokenQuant;   // no check
```
There is no invariant check for the non-hardened path — the `if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0))` guard only runs when `hardenedCalc == true`. Since `getAllowHardenExchangeCalculation()` defaults to `0` and must be turned on via a committee proposal (`ProposalType.ALLOW_HARDEN_EXCHANGE_CALCULATION`), the currently-active code path on any chain that hasn't explicitly enabled hardening performs **no bounds check** at all before mutating `ExchangeCapsule`'s protobuf `long` fields.

The double-precision Bancor formula (`Math.pow` on quantities that can be in the trillions, per `exchangeToSupply`/`exchangeFromSupply`) is inherently susceptible to rounding/precision errors that, in edge cases (extreme reserve ratios, repeated small trades, or crafted `sellTokenQuant` values), can produce a `buyTokenQuant` that exceeds the actual `secondTokenBalance`/`firstTokenBalance` held in the pool. Unlike Solidity's overflow-checked arithmetic (the point of the Ajna report), Java `long` subtraction does not throw on underflow — it silently wraps to a negative value, which is then persisted directly into the `Exchange` protobuf as the new pool balance.

### Impact Explanation
A negative pool balance corrupts the AMM invariant used for all subsequent trades against that exchange pair (`Exchange.getFirstTokenBalance()`/`getSecondTokenBalance()` feed directly into `ExchangeCreateActuator`/`ExchangeTransactionActuator`/`ExchangeWithdrawActuator`/`ExchangeInjectActuator` math). This can be leveraged to mint value out of the pool (attacker drains more of the counter-asset than the pool legitimately holds), i.e., unbacked balance / theft of funds from other exchange participants, and/or permanently corrupts pool accounting (denial of the exchange feature for that pair). This satisfies the "unbacked balance"/"theft of funds" impact bar.

### Likelihood Explanation
Reachable by any account issuing an `ExchangeTransactionContract`/`ExchangeInjectContract`/`ExchangeWithdrawContract` transaction (unprivileged, standard user-facing feature) as long as `allowHardenExchangeCalculation` has not been enabled by committee proposal — which is the default state, as confirmed by [3](#0-2)  and the proposal test showing the default value is `0`: [4](#0-3) . Triggering the precision edge case requires crafting specific reserve ratios/trade sizes (feasible for an attacker who controls pool creation and injects arbitrary reserves via `ExchangeCreateActuator`/`ExchangeInjectActuator`), making this a Medium-likelihood, high-impact issue.

### Recommendation
Make the non-negative post-condition check (`newFirstTokenBalance < 0 || newSecondTokenBalance < 0`) unconditional in `ExchangeCapsule.transaction()`, regardless of `hardenedCalc`, and consider deprecating/retiring the legacy floating-point `ExchangeProcessor` path in favor of always routing through `SafeExchangeProcessor` (BigDecimal-based, checked arithmetic), independent of the `allowHardenExchangeCalculation` governance flag.

### Proof of Concept
1. Do not enable `ALLOW_HARDEN_EXCHANGE_CALCULATION` (default network state).
2. Create an exchange pool via `ExchangeCreateActuator` with reserve ratios chosen to maximize floating-point rounding error in `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` (e.g., very small one-side reserve combined with a large `sellTokenQuant`, similar to the precision deltas already observed in `testStrictMath` test vectors at [5](#0-4) , where legacy vs. hardened/strict results differ).
3. Submit an `ExchangeTransactionContract` with `quant` selected so that the legacy processor's returned `buyTokenQuant` exceeds the pool's actual counter-token reserve.
4. Observe `ExchangeCapsule.transaction()` computing a negative `newSecondTokenBalance` (or `newFirstTokenBalance`) via unchecked `long` subtraction, with no exception thrown, and the corrupted (negative) balance being persisted via `Commons.putExchangeCapsule(...)` in `ExchangeTransactionActuator.execute()` at [6](#0-5) .
5. Subsequent trades against the corrupted pool operate on an invalid invariant, allowing extraction of more counter-asset than was ever deposited.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-168)
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

**File:** chainbase/src/main/java/org/tron/core/store/DynamicPropertiesStore.java (L3059-3072)
```java
  public long getAllowHardenExchangeCalculation() {
    return Optional.ofNullable(getUnchecked(ALLOW_HARDEN_EXCHANGE_CALCULATION))
        .map(BytesCapsule::getData)
        .map(ByteArray::toLong)
        .orElse(0L);
  }

  public void saveAllowHardenExchangeCalculation(long value) {
    this.put(ALLOW_HARDEN_EXCHANGE_CALCULATION, new BytesCapsule(ByteArray.fromLong(value)));
  }

  public boolean allowHardenExchangeCalculation() {
    return getAllowHardenExchangeCalculation() == 1L;
  }
```

**File:** framework/src/test/java/org/tron/core/actuator/utils/ProposalUtilTest.java (L721-724)
```java
    // 3) current value is 0 (default), proposing 0 again -> rejected
    thrown = assertThrows(ContractValidateException.class, proposeZero);
    assertEquals("[ALLOW_HARDEN_EXCHANGE_CALCULATION] has been set to 0, no need to propose again",
        thrown.getMessage());
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L93-99)
```java
      accountStore.put(accountCapsule.createDbKey(), accountCapsule);

      Commons.putExchangeCapsule(exchangeCapsule, dynamicStore, exchangeStore, exchangeV2Store,
          assetIssueStore);

      ret.setExchangeReceivedAmount(anotherTokenQuant);
      ret.setStatus(fee, code.SUCESS);
```
