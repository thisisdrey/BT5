### Title
Permissionless TRC10 tokens can be paired in the Bancor-style Exchange pool and, via un-hardened floating point arithmetic, permanently corrupt/lock the pool's balances - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java`)

### Summary
Any account can permissionlessly issue a TRC10 token (`AssetIssueContract`) with an attacker-chosen precision/total supply and then permissionlessly create or inject into a bancor-formula liquidity pool (`ExchangeCreateContract` / `ExchangeInjectContract` / `ExchangeTransactionContract`). Unless the on-chain proposal `allowHardenExchangeCalculation` is active, all balance updates in `ExchangeCapsule.transaction()` use unchecked `long` arithmetic and the swap-rate itself is computed with `double`/`long` casts in `ExchangeProcessor`, mirroring the reported bug class: an unprivileged token issuer engineering reserve ratios/decimals that push the internal calculation near an overflow boundary, corrupting pool state and denying withdrawals to legitimate depositors.

### Finding Description
`ExchangeCapsule.transaction()` selects the calculation engine based on the `hardenedCalc` flag: [1](#0-0) 

When `hardenedCalc` is `false` (the legacy/default path when the `allowHardenExchangeCalculation` proposal has not been activated), reserve updates use plain `long + `/`- ` with no overflow check and no post-condition validation (`newFirstTokenBalance < 0` is only checked in the hardened branch): [2](#0-1) 

The swap amount itself is computed by `ExchangeProcessor`, which relies on `double` arithmetic and a raw cast `(long) issuedSupply` / `(long) exchangeBalance`, with no bounds checking: [3](#0-2) 

Both the pool's `firstTokenBalance`/`secondTokenBalance` and the swap quantity are attacker-influenced through fully permissionless actions:
- `ExchangeCreateActuator.execute()` lets any account set arbitrary `firstTokenBalance`/`secondTokenBalance` for a newly issued asset (subject only to `getExchangeBalanceLimit()`), pairing it with TRX or another token. [4](#0-3) 
- `ExchangeInjectActuator.execute()` lets any account further inflate the reserves. [5](#0-4) 
- `ExchangeTransactionActuator.execute()` performs the swap and persists the (potentially corrupted) reserves via `Commons.putExchangeCapsule`. [6](#0-5) 

This is structurally the same bug class as the report: a permissionless asset/pool operation feeds attacker-controlled numeric inputs into a reward/rate calculation whose overflow/precision handling is not hardened by default, and once the internal state (reserve/`rewardPerTokenStored`-equivalent) is corrupted, further pool operations either revert (denial of service) or compute incorrect payouts (loss of funds), and honest depositors can no longer safely withdraw. The codebase already contains an internal "hardened" remediation (`SafeExchangeProcessor`, `StrictMathWrapper.addExact/subtractExact`, negative-balance guard) gated behind the `allowHardenExchangeCalculation` dynamic property, which strongly indicates this exact overflow/precision class was previously identified as needing a fix — but the legacy, unguarded path remains reachable and is exercised whenever that proposal parameter is not enabled.

### Impact Explanation
If the legacy path is active (i.e., the hardening proposal has not been activated on the network), a malicious, permissionless combination of TRC10 issuance + exchange creation/injection + crafted swap quantities can drive the `long` reserve fields negative/overflowed or make `ExchangeProcessor`'s double-based computation return corrupted values that get persisted via `Commons.putExchangeCapsule`. Once corrupted, subsequent `ExchangeTransactionActuator`/`ExchangeWithdrawActuator` operations against that pool can behave incorrectly (miscalculated payouts) or begin throwing, freezing the pool's TRX/TRC10 reserves and locking out all participants — matching the report's "permanent lock of the pool and loss of funds for honest users."

### Likelihood Explanation
Every step is reachable by a single unprivileged account via three ordinary, fee-paying broadcast transactions (`AssetIssueContract`, `ExchangeCreateContract`/`ExchangeInjectContract`, `ExchangeTransactionContract`) — no special privilege, SR/witness role, or node compromise is required. The only mitigating factor is whether `allowHardenExchangeCalculation` has been activated network-wide via committee proposal; I was unable to confirm from the indexed code whether this proposal is currently active on mainnet, so the real-world likelihood depends on that governance state, which is outside what static code inspection can determine.

### Recommendation
Make the hardened, overflow-checked exchange calculation path (`SafeExchangeProcessor` + `StrictMathWrapper.addExact/subtractExact` + the `newFirstTokenBalance/newSecondTokenBalance < 0` guard) the unconditional default rather than an opt-in proposal, and add reserve/precision sanity limits at `ExchangeCreateActuator`/`ExchangeInjectActuator` validation time so that assets with pathological precision/supply combinations cannot be paired into a pool in the first place.

### Proof of Concept
Concrete PoC values were not present in the indexed test suite for the un-hardened path; existing hardened-mode tests (`ExchangeCapsuleTest.testHardenedTransactionNegativeBalanceThrows`, `ExchangeProcessorTest.testHardenedOverflowDetection`) demonstrate that overflow conditions are constructible in this exact code path but are only rejected when the hardened flag is enabled: [7](#0-6) [8](#0-7) 
Reproducing the equivalent scenario without setting `allowHardenExchangeCalculation` (i.e., using the default `ExchangeCapsule.transaction(..., false)` / plain `ExchangeProcessor` path) would need to be validated in a live/test node to confirm the default proposal state, which requires execution capability beyond static code search.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-145)
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

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L146-166)
```java

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L55-91)
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
        exchangeStore.put(exchangeCapsule.createDbKey(), exchangeCapsule);
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

**File:** framework/src/test/java/org/tron/core/capsule/utils/ExchangeProcessorTest.java (L159-163)
```java
  @Test
  public void testHardenedOverflowDetection() {
    assertThrows(ArithmeticException.class, () ->
        SafeExchangeProcessor.INSTANCE.exchange(Long.MAX_VALUE, 1_000_000L, 1L));
  }
```
