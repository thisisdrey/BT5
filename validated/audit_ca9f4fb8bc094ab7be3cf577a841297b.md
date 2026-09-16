## Finding

### Title
Non-hardened Exchange (Bancor pool) token balance can silently underflow to a negative value in `ExchangeCapsule.transaction()` - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java`)

### Summary
This finding is analogous to the reported `totalDeposited` underflow: java-tron's on-chain Bancor-style exchange pool (`ExchangeCreateContract` / `ExchangeInjectContract` / `ExchangeTransactionContract`) keeps its own accounting variables (`firstTokenBalance`, `secondTokenBalance`) that are supposed to always stay `>= 0`, but by default the code path that updates them does a raw subtraction with no underflow check, exactly like the reported `totalDeposited -= amount` pattern.

### Finding Description
`ExchangeCapsule.transaction()` computes a `buyTokenQuant` using a floating point Bancor-formula implementation (`ExchangeProcessor`, using `Maths.pow`/`double` arithmetic) and then updates the pool balances: [1](#0-0) 

When `hardenedCalc` is `false` (the default), the new balances are computed with plain `long` subtraction/addition and **no check at all** that the result is non-negative:
```
newFirstTokenBalance = firstTokenBalance - buyTokenQuant;   // no check
newSecondTokenBalance = secondTokenBalance + sellTokenQuant;
```
Only when `hardenedCalc` is `true` does the code validate `newFirstTokenBalance < 0 || newSecondTokenBalance < 0` and throw `ContractValidateException`: [2](#0-1) 

`hardenedCalc` is driven by `allowHardenExchangeCalculation()`, a dynamic/proposal-controlled chain parameter set via `AbstractExchangeActuator.allowHarden()`: [3](#0-2) 

This flag is only enabled through a super-representative committee proposal (`ProposalUtil.java`, `ProposalService.java`), so on any chain/network where the proposal hasn't been activated, every `ExchangeTransactionContract` broadcast by an ordinary, unprivileged account goes through the **unchecked** subtraction path: [4](#0-3) 

Because `ExchangeProcessor.exchangeFromSupply()` relies on `double` floating-point math (`Maths.pow`), the returned `buyTokenQuant` is not guaranteed to be bounded by the true integer pool balance in all edge cases (e.g., extreme ratios, very small/large balances, or reserve balances near the boundary after repeated trades). If `buyTokenQuant` (or accumulated rounding across many trades) ever exceeds the actual token balance held in the pool, `newFirstTokenBalance`/`newSecondTokenBalance` silently goes negative and is persisted via `setBalance()`, corrupting the pool's on-chain accounting exactly as described in the report for `totalDeposited`.

### Impact Explanation
A corrupted (negative or otherwise inconsistent) pool balance breaks all subsequent `ExchangeInjectActuator`/`ExchangeWithdrawActuator`/`ExchangeTransactionActuator` calculations that rely on `firstTokenBalance`/`secondTokenBalance` as the source of truth for the pool's real token reserves. This can result in some traders being unable to withdraw their rightful share (transactions reverting due to insufficient/negative computed balances) while others drain more than their fair share, i.e., permanent freezing of funds for some users and unbacked/incorrect balance for others — matching the "funds stuck in the pool" impact class in the original report.

### Likelihood Explanation
Reachable by any account that can submit an `ExchangeTransactionContract` (a normal, unprivileged, broadcastable transaction type) against an existing exchange pool created via `ExchangeCreateContract`. No special privilege is required, and the vulnerable arithmetic path (`hardenedCalc == false`) is the **default** unless the network's SRs have explicitly enabled `allowHardenExchangeCalculation` via governance proposal. Triggering an actual underflow requires driving the pool into edge-case ratios (e.g., very small reserve balance vs. large trade size or many repeated micro-trades) to exploit floating-point rounding in `ExchangeProcessor`, which is plausible for an attacker with sufficient trade volume/repeated transactions but not trivially guaranteed on a single call, keeping likelihood at Medium.

### Recommendation
Apply the existing hardened check unconditionally rather than gating it behind `allowHardenExchangeCalculation`: always validate `newFirstTokenBalance >= 0 && newSecondTokenBalance >= 0` in `ExchangeCapsule.transaction()` before persisting, regardless of the `hardenedCalc` flag, and throw `ContractValidateException`/`ContractExeException` on violation. Consider also bounding/asserting the floating point results against exact integer arithmetic to avoid precision-driven balance drift.

### Proof of Concept
Not independently exploited in a live environment; identified via static analysis of `ExchangeCapsule.transaction()` at [5](#0-4)  compared against the guarded hardened path, plus confirmation that `allowHardenExchangeCalculation` defaults to disabled and is only flipped via SR proposal (`ProposalUtil.java`, `ProposalService.java`), meaning the unchecked arithmetic executes for ordinary `ExchangeTransactionContract` transactions today.

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
