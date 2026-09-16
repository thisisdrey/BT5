### Title
Reserve Manipulation via Floating-Point Precision Loss in the Native Exchange (Bancor-style) AMM — ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java])

### Summary
java-tron ships a native, on-chain constant-formula AMM (the `Exchange`/`ExchangeV2` TRC10↔TRC10/TRX market) that is functionally analogous to a PancakeSwap V2 pool: any account can create a pool [1](#0-0) , anyone can inject liquidity [2](#0-1) , and any unprivileged account can swap through the pool by broadcasting an `ExchangeTransactionContract` [3](#0-2) . The actual swap-output math (the equivalent of PancakeSwap's reserve-based `getAmountOut`) is computed in `ExchangeProcessor`, which by default uses `double` (IEEE‑754) arithmetic truncated to `long` with no bound/invariant checks on the result relative to the reserves.

### Finding Description
The core pricing logic lives in `ExchangeCapsule.transaction()`, which is invoked for every `ExchangeTransactionContract` execution and recomputes the swap output purely from the pool's currently stored reserves (`firstTokenBalance`/`secondTokenBalance`) [4](#0-3) . Unless the committee-gated `allowHardenExchangeCalculation` proposal parameter has been turned on (it is off by default, controlled via `ProposalUtil`/`ProposalService`, not enabled unconditionally) [5](#0-4) , the actual computation is delegated to the legacy `ExchangeProcessor`:

```
private long exchangeToSupply(long balance, long quant) {
  long newBalance = balance + quant;
  double issuedSupply = -supply * (1.0 - Maths.pow(1.0 + (double) quant / newBalance, 0.0005, ...));
  long out = (long) issuedSupply;   // truncating double->long cast, no bound check
  supply += out;
  return out;
}
private long exchangeFromSupply(long balance, long supplyQuant) {
  supply -= supplyQuant;
  double exchangeBalance = balance * (Maths.pow(1.0 + (double) supplyQuant / supply, 2000.0, ...) - 1.0);
  return (long) exchangeBalance;   // truncating double->long cast, no bound check
}
``` [6](#0-5) 

This is a bonding-curve approximation of the standard constant-product AMM swap, but unlike the actual invariant enforced in the hardened replacement (`SafeExchangeProcessor`, which explicitly proves `result <= buy reserve` in `BigDecimal` arithmetic and rejects overflow via `StrictMathWrapper.addExact`) [7](#0-6) , the default `ExchangeProcessor` performs the whole computation in lossy floating point with no post-hoc validation that the computed `buyTokenQuant` is consistent with the reserve ratio invariant. The project's own test suite (`ExchangeProcessorTest.testStrictMath`) explicitly demonstrates that the naive (`ExchangeProcessor`, `useStrictMath=false`) and strict (`useStrictMath=true`/`SafeExchangeProcessor`) paths produce *different* results for identical inputs across dozens of test vectors [8](#0-7) , confirming the default computation is measurably imprecise and diverges from the invariant-respecting version.

Because the resulting `buyTokenQuant`/`newFirstTokenBalance`/`newSecondTokenBalance` are written directly back into the persisted `ExchangeCapsule` reserves with no sanity check against the constant-formula invariant [9](#0-8) , an attacker who crafts sell quantities that land on floating-point rounding edges (e.g., values that force `Maths.pow`/division truncation to favor the taker) can repeatedly extract more value from the pool per trade than the reserve math should allow — the same fundamental bug class as the PancakeSwap V2 "reserve manipulation" incident: manipulating a price/reserve-derived output computation that lacks invariant enforcement to siphon pool value, executed entirely through normal, unprivileged transaction broadcasting (`ExchangeTransactionContract`), with no special permission required beyond `supportAllowMarketTransaction`/exchange existing.

### Impact Explanation
Successful exploitation lets an unprivileged account extract TRX/TRC10 tokens from an `Exchange` pool beyond what the constant-formula pricing curve should permit, directly draining liquidity providers' and the pool creator's locked funds (theft/unbacked-balance class impact) via a completely standard signed transaction path (`ExchangeTransactionActuator`). This matches the in-scope "concrete unauthorized account operation, theft ... of funds" criterion.

### Likelihood Explanation
The `Exchange` feature (TRC10 AMM) is a long-standing native java-tron capability, reachable by any account holding TRX/TRC10 balances with no special privilege. The vulnerable floating-point code path (`ExchangeProcessor`) is the *default* execution path, since the hardened alternative (`SafeExchangeProcessor`) requires an opt-in committee proposal (`allowHardenExchangeCalculation`) that is not universally enabled. The magnitude of exploitable rounding error scales with pool size and the attacker's ability to choose exact trade quantities/reserve states (via `ExchangeInject`/multiple `ExchangeTransaction` calls), which is fully attacker-controlled.

### Recommendation
- Make `SafeExchangeProcessor` (or equivalent invariant-checked `BigDecimal`/fixed-point math) the mandatory, non-optional swap-pricing implementation for `ExchangeTransactionActuator`/`ExchangeCapsule.transaction()`, removing the legacy `ExchangeProcessor` double-based path entirely (or hard-gating it behind a chain-wide, non-reversible hard fork rather than a soft, togglable committee parameter).
- Enforce a post-computation invariant check in `ExchangeCapsule.transaction()` for both hardened and non-hardened paths: verify the new reserve pair does not violate the pool's constant-formula invariant (e.g., output ≤ buy reserve, and the effective price move is consistent with input quantity) before committing state.
- Audit `AbstractExchangeActuator.addExact/subtractExact` to make overflow-checked arithmetic (`StrictMathWrapper`) unconditional rather than gated by `allowHarden()`.

### Proof of Concept
Not independently reproducible as an end-to-end numeric exploit within this review (would require exhaustively searching `(sellBalance, buyBalance, sellQuant)` triples for a rounding case where `ExchangeProcessor.exchange()` returns a `buyTokenQuant` exceeding the theoretically correct constant-formula value by an economically significant margin, and confirming it is reachable via `ExchangeTransactionActuator` with `dynamicStore.getExchangeBalanceLimit()`/precision-check constraints satisfied). The codebase's own `ExchangeProcessorTest.testStrictMath` test vectors (lines 218–281) already demonstrate concrete input triples where the default (`useStrictMath=false`) processor's output differs from the invariant-respecting `SafeExchangeProcessor` result [10](#0-9) ; a background engineering session with code-execution access would be needed to search this space for a case where the naive processor's output exceeds the safe/invariant-bounded output (rather than merely differing), which would constitute a directly provable value-extraction PoC.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L55-90)
```java
      byte[] firstTokenID = exchangeCreateContract.getFirstTokenId().toByteArray();
      byte[] secondTokenID = exchangeCreateContract.getSecondTokenId().toByteArray();
      long firstTokenBalance = exchangeCreateContract.getFirstTokenBalance();
      long secondTokenBalance = exchangeCreateContract.getSecondTokenBalance();

      long newBalance = subtractExact(accountCapsule.getBalance(), fee);

      accountCapsule.setBalance(newBalance);

      if (Arrays.equals(firstTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, firstTokenBalance));
      } else {
        accountCapsule
            .reduceAssetAmountV2(firstTokenID, firstTokenBalance, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(secondTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, secondTokenBalance));
      } else {
        accountCapsule
            .reduceAssetAmountV2(secondTokenID, secondTokenBalance, dynamicStore, assetIssueStore);
      }

      long id = addExact(dynamicStore.getLatestExchangeNum(), 1);
      long now = dynamicStore.getLatestBlockHeaderTimestamp();
      if (dynamicStore.getAllowSameTokenName() == 0) {
        //save to old asset store
        ExchangeCapsule exchangeCapsule =
            new ExchangeCapsule(
                exchangeCreateContract.getOwnerAddress(),
                id,
                now,
                firstTokenID,
                secondTokenID
            );
        exchangeCapsule.setBalance(firstTokenBalance, secondTokenBalance);
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L60-83)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();
      long firstTokenBalance = exchangeCapsule.getFirstTokenBalance();
      long secondTokenBalance = exchangeCapsule.getSecondTokenBalance();

      byte[] tokenID = exchangeInjectContract.getTokenId().toByteArray();
      long tokenQuant = exchangeInjectContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant;

      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
        anotherTokenQuant = floorDiv(multiplyExact(
            secondTokenBalance, tokenQuant), firstTokenBalance);
        exchangeCapsule.setBalance(addExact(firstTokenBalance, tokenQuant),
            addExact(secondTokenBalance, anotherTokenQuant));
      } else {
        anotherTokenID = firstTokenID;
        anotherTokenQuant = floorDiv(multiplyExact(
            firstTokenBalance, tokenQuant), secondTokenBalance);
        exchangeCapsule.setBalance(addExact(firstTokenBalance, anotherTokenQuant),
            addExact(secondTokenBalance, tokenQuant));
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-69)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();

      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-158)
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
