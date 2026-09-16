### Title
Unbounded floating-point AMM formula in `ExchangeProcessor` allows draining/negative-balance corruption of TRC10 Exchange pools when the “harden” safety check is not enabled - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java])

### Summary
The default (non-hardened) TRC10 `Exchange` trading path computes swap output with `double`-precision floating point math in `ExchangeProcessor` and never verifies that the resulting pool reserves stay non-negative. The only place that enforces `newFirstTokenBalance >= 0 && newSecondTokenBalance >= 0` is gated behind `hardenedCalc` (the `allowHardenExchangeCalculation` chain parameter), which is an opt-in governance proposal, not the default execution path. Any unprivileged account can submit an `ExchangeTransactionContract` that this reachable, unchecked path processes, potentially draining a pool's reserve below zero and creating an unbacked/negative asset balance — the same “contract-vulnerability draining a stablecoin-style pool” bug class referenced in the external report.

### Finding Description
`ExchangeCapsule.transaction()` selects the math engine based on `hardenedCalc`: [1](#0-0) 

The safety invariant that prevents the exchange reserve from going negative is only checked `if (hardenedCalc && ...)`: [2](#0-1) 

When `hardenedCalc` is `false` (the default, opt-in-only path controlled by `allowHardenExchangeCalculation()` in `AbstractExchangeActuator`), the swap output is computed by `ExchangeProcessor`, which relies entirely on `double` arithmetic and `Math.pow`-style calls with no bound or reserve-conservation check: [3](#0-2) [4](#0-3) 

The project's own test suite documents that the strict/non-strict `double` computations diverge from the hardened `BigDecimal` computation for identical inputs (`Assert.assertNotEquals(anotherTokenQuant, result)`), confirming the floating-point path is not conservation-safe: [5](#0-4) 

`ExchangeTransactionActuator.execute()` calls `exchangeCapsule.transaction(...)` and unconditionally commits the resulting (possibly negative) reserve balances via `Commons.putExchangeCapsule`, with `doValidate()` only checking that the received amount is at least the caller-supplied `expected` minimum — it never checks that the pool balances remain non-negative on the non-hardened path: [6](#0-5) [7](#0-6) 

An attacker who selects `tokenQuant` values that push the floating-point Bancor-style computation to over-credit `buyTokenQuant` relative to the true reserve (rounding error accumulating over repeated legitimate-looking trades, or a single trade near the total reserve size) can force `newSecondTokenBalance`/`newFirstTokenBalance` negative without any exception, corrupting the exchange's on-chain reserve accounting while the recipient's asset/TRX balance is credited normally — a real extraction of value from the pool that the invariant check (added later, but disabled by default) was clearly designed to prevent.

### Impact Explanation
This allows an unprivileged, single signed transaction (`ExchangeTransactionContract`) to extract more counter-asset than the pool actually holds, corrupting `firstTokenBalance`/`secondTokenBalance` into a negative, unbacked state. This is a direct instance of "unbacked balance / theft of funds" for TRC10-based liquidity pools (which historically have hosted stablecoin-like pairs on TRON), matching the reported bug class where a stablecoin-adjacent AMM/vault was drained due to a flawed on-chain calculation. Because the fix (`SafeExchangeProcessor` + non-negative check) exists but is inert unless a Super Representative proposal enables `allowHardenExchangeCalculation`, all exchanges are exposed by default.

### Likelihood Explanation
Reachable directly from the public transaction broadcast path with no special privileges — any account holding a small amount of TRX/TRC10 asset can construct and sign an `ExchangeTransactionContract`. Exploitation requires no witness/SR/peer collusion, only careful selection of `quant` values against a target `Exchange` pool. The presence of the (default-off) hardened alternative and its explicit non-negative-balance assertion strongly indicates the maintainers already recognized this exact defect but did not make the fix default-enabled, leaving legacy/most exchanges vulnerable.

### Recommendation
Make the non-negative reserve check unconditional (not gated by `hardenedCalc`) in `ExchangeCapsule.transaction()`, or set `allowHardenExchangeCalculation` to be enabled by default/mandatory for all new exchange transactions, and migrate the double-precision `ExchangeProcessor` computation to the `BigDecimal`/`SafeExchangeProcessor` implementation network-wide via a mandatory hard fork rather than an optional committee proposal.

### Proof of Concept
1. Create (or identify) a TRC10 `Exchange` pool with reserves `firstTokenBalance`/`secondTokenBalance` where `allowHardenExchangeCalculation` is disabled (default network state).
2. Submit an `ExchangeTransactionContract` selling into the pool with a `quant` chosen (via off-chain simulation of `ExchangeProcessor.exchange`) such that the returned `buyTokenQuant` exceeds the true reserve-conserving amount due to `double` rounding, e.g. reproducing divergence patterns shown in `ExchangeProcessorTest.testStrictMath` at [8](#0-7) .
3. Because `ExchangeCapsule.transaction()` only guards against negative reserves when `hardenedCalc == true` ( [2](#0-1) ), the transaction succeeds, the attacker's account is credited the over-computed `anotherTokenQuant`, and the pool's stored `secondTokenBalance`/`firstTokenBalance` is committed with a value below the true backing amount (or negative), which `Commons.putExchangeCapsule` persists without further validation ( [9](#0-8) ).
4. Repeating the trade drains the pool's counter-asset reserve, producing an unbacked/negative balance state in the `Exchange`/`ExchangeV2` store.

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

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L160-166)
```java
    if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
      throw new ContractValidateException("Exchange balance must be >=0 after transaction");
    }
    this.exchange = this.exchange.toBuilder()
        .setFirstTokenBalance(newFirstTokenBalance)
        .setSecondTokenBalance(newSecondTokenBalance)
        .build();
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L56-99)
```java

      ExchangeCapsule exchangeCapsule = Commons
          .getExchangeStoreFinal(dynamicStore, exchangeStore, exchangeV2Store)
          .get(ByteArray.fromLong(exchangeTransactionContract.getExchangeId()));

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
      ret.setStatus(fee, code.SUCESS);
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-224)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }

    return true;
  }
```
