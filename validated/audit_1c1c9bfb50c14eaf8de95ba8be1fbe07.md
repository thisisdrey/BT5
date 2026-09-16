### Title
Legacy (non-hardened) Exchange bonding-curve path allows an Exchange pool's reserve to go negative and credits users with tokens the pool does not hold - (File: chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java)

### Summary
`ExchangeTransactionActuator`/`ExchangeCapsule.transaction()` computes the counter-token amount using either a legacy floating-point `ExchangeProcessor` or a `SafeExchangeProcessor`, chosen by the `allowHardenExchangeCalculation` dynamic property. Only the hardened path validates that the resulting pool reserves stay non-negative; the legacy path performs no such check, so an attacker can force a pool's reserve accounting negative while the actuator still credits the full swapped-out amount to the trader's account balance — the same "swap without limits" root cause described in the external Lyra report (`revertBuyOnInsufficientFunds=false` letting a pool over-pay from a reserve it doesn't have).

### Finding Description
`ExchangeCapsule.transaction()` selects the calculation engine based on the `hardenedCalc` flag: [1](#0-0) 

Only `if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0))` throws; when `hardenedCalc` is `false` (the legacy, double-precision `ExchangeProcessor` path), no invariant check is performed and the resulting reserve balances are stored as-is, even if negative.

`hardenedCalc` comes from `AbstractExchangeActuator.allowHarden()`, which simply reads the dynamic property `allowHardenExchangeCalculation()`: [2](#0-1) 

This is a chain-wide parameter that must be turned on by committee proposal; until it is active (or on any network/side-chain where it is left at its default/off state), every `ExchangeTransactionContract`/`ExchangeTransactionActuator.execute()` call runs through the unchecked legacy `ExchangeProcessor`: [3](#0-2) 

`ExchangeTransactionActuator.doValidate()` only bounds the balance of the token being **sold** against `getExchangeBalanceLimit()`, and only checks that the computed `anotherTokenQuant` is **at least** `tokenExpected` (a user-supplied slippage floor) — it never checks that `anotherTokenQuant` is less than or equal to the pool's actual reserve of the token being bought: [4](#0-3) 

The legacy `ExchangeProcessor.exchange()` computes the counter-amount purely with `double` arithmetic (`Maths.pow`), which is exactly the kind of calculation the project's own test suite (`ExchangeProcessorTest`) shows diverges from the `BigDecimal`-based `SafeExchangeProcessor` for the same inputs (`testStrictMath` explicitly asserts the two differ): [5](#0-4) [6](#0-5) 

Because the project's own `ExchangeCapsuleTest.testHardenedTransactionNegativeBalanceThrows` was written specifically to demonstrate that the hardened path throws on negative reserves — implying the non-hardened path does **not** throw and can silently persist a negative-balance exchange: [7](#0-6) 

When the legacy path drives a reserve to (or effectively past) zero/negative through floating-point rounding on the buy side, `ExchangeTransactionActuator.execute()` still unconditionally credits the full `anotherTokenQuant` to the trader's account via `addAssetAmountV2`/`setBalance`, exactly mirroring the Lyra bug pattern where `_maybeExchangeBase(..., revertBuyOnInsufficientFunds=false)` let the pool swap out funds it didn't have: [8](#0-7) 

### Impact Explanation
An attacker submitting a single, unprivileged `ExchangeTransactionContract` (broadcastable by any signed transaction, no special privileges needed) can drive an Exchange pool's on-chain reserve accounting negative while the actuator still pays out the computed counter-token amount from `AccountStore`/`AssetIssueStore`. This creates an unbacked-token credit to the attacker's account (the exchange's recorded reserve for that token no longer matches, or exceeds, the token supply actually held/backed by the pool), and subsequent legitimate `ExchangeTransaction`/`ExchangeWithdraw` operations against that exchange will operate on corrupted (negative) reserve state, potentially causing further miscalculated payouts or unexpected exceptions for other users. This matches the "unbacked balance" / "theft of funds" impact class.

### Likelihood Explanation
Reachable directly via a single broadcast transaction (`ExchangeTransactionContract`) with no special permissions, as long as the network/committee has not activated `allowHardenExchangeCalculation`. The relevant checks (`balanceLimit`, `tokenExpected`) are on the sell-side reserve and floor amount, not on the buy-side reserve sufficiency, so exploitation only requires the attacker to construct extreme (but within `ExchangeBalanceLimit`) buy/sell amounts to induce floating-point divergence in the legacy `ExchangeProcessor`, as already demonstrated by the project's own tests showing legacy vs. hardened divergence for numerous realistic inputs.

### Recommendation
Make the negative-balance / underflow guard in `ExchangeCapsule.transaction()` unconditional (not gated by `hardenedCalc`), or force `hardenedCalc` to always be used regardless of `allowHardenExchangeCalculation`, so both legacy and hardened arithmetic reject any exchange whose result would push either pool reserve at or below zero. Additionally, add an explicit validation in `ExchangeTransactionActuator.doValidate()` that `anotherTokenQuant` does not exceed the current reserve of the token being bought.

### Proof of Concept
Not independently executed; conceptually confirmed by the codebase's own regression tests:
- `ExchangeCapsuleTest.testHardenedTransactionNegativeBalanceThrows` (only the hardened path enforces the non-negative invariant). [7](#0-6) 
- `ExchangeProcessorTest.testStrictMath` shows the legacy `ExchangeProcessor` (double math) produces results different from `SafeExchangeProcessor`/strict-math for the same inputs, confirming rounding divergence exists in production code paths when hardening is not enabled. [9](#0-8) 

Note: I could not confirm from the index whether `allowHardenExchangeCalculation` is enabled by default on TRON mainnet at the current chain height (this depends on committee proposal history, which is runtime chain state, not something visible in the static repository). If it is already permanently active on mainnet, this issue is latent/non-exploitable there but remains exploitable on any deployment (private chain, testnet, sidechain) where the proposal has not been activated.

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

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-15)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-99)
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

      ret.setExchangeReceivedAmount(anotherTokenQuant);
      ret.setStatus(fee, code.SUCESS);
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

**File:** framework/src/test/java/org/tron/core/capsule/utils/ExchangeProcessorTest.java (L218-270)
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

**File:** framework/src/test/java/org/tron/core/capsule/ExchangeCapsuleTest.java (L71-83)
```java
  @Test
  public void testHardenedTransactionNegativeBalanceThrows() throws Exception {
    // Construct a corrupt-state pool with a negative balance to drive the
    // < 0 invariant in the hardened branch via subtractExact wrapping.
    ExchangeCapsule capsule = new ExchangeCapsule(
        ByteString.copyFromUtf8("owner"), 99L, 0L,
        "abc".getBytes(), "def".getBytes());
    capsule.setBalance(Long.MAX_VALUE, 1L);

    // Selling abc adds to firstTokenBalance: addExact(MAX, q) overflows -> ArithmeticException
    Assert.assertThrows(ArithmeticException.class,
        () -> capsule.transaction("abc".getBytes(), 1L, true, true));
  }
```
