### Title
Missing Slippage Protection in `ExchangeInjectActuator` Enables Front-Running/Sandwich Manipulation of AMM Pool Ratio - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java])

### Summary
Like the reported `buyCollateralFromAuction` bug (a swap-style function that trusts a price derived from external/mutable state with no bound on acceptable output), java-tron's Bancor-style `Exchange` (`ExchangeStore`/`ExchangeV2Store`) computes token ratios purely from the pool's own on-chain reserves (`firstTokenBalance` / `secondTokenBalance`). `ExchangeTransactionActuator` protects callers with an explicit `expected` minimum-output check, but the sibling `ExchangeInjectActuator`, which computes the paired-token amount from the very same manipulable reserves, has no equivalent bound.

### Finding Description
`ExchangeInjectActuator.doValidate()`/`execute()` computes how much of the paired token must be pulled from (or credited to) the injector using the live pool ratio: [1](#0-0) 

This ratio (`firstTokenBalance`/`secondTokenBalance`) is the same mutable state used by `ExchangeCapsule.transaction()`/`ExchangeProcessor.exchange()`, and it can be moved arbitrarily within the balance limits by any unprivileged user submitting an `ExchangeTransactionContract` right before the injection transaction is applied: [2](#0-1) [3](#0-2) 

Unlike `ExchangeInjectActuator`, `ExchangeTransactionActuator` explicitly protects the caller by requiring a caller-supplied minimum output (`tokenExpected`) and reverting if the freshly-computed swap output would be worse: [4](#0-3) 

`ExchangeInjectActuator` has no analogous field/check — it only validates that the computed `anotherTokenQuant` is `> 0` and within the global balance limit, never that it matches what the injector intended: [5](#0-4) 

Because block producers apply transactions in the order they are received/selected, an unprivileged transaction broadcaster can submit an `ExchangeTransactionContract` that skews `firstTokenBalance`/`secondTokenBalance` immediately ahead of a pending `ExchangeInjectContract`, then submit a reversing trade immediately after, classic sandwich/front-running against the AMM ratio — exactly the "manipulated price used for unprotected order matching" bug class described in the report.

### Impact Explanation
The exchange creator (the only account authorized to call `ExchangeInjectContract`, per the creator check at `ExchangeInjectActuator.java:175-177`) can be forced to inject at a manipulated, unfavorable ratio, causing them to contribute a disproportionate amount of one asset relative to the other, or receive a much smaller-than-expected credit of the paired token balance recorded in the pool. This is a concrete, unauthorized value transfer out of the victim (the liquidity injector) to the attacker who reverses the sandwich trade afterward, i.e., theft of funds via price manipulation, matching the Medium severity of the referenced report.

### Likelihood Explanation
The attack requires only ordinary, unprivileged transactions (`ExchangeTransactionContract` calls) that any account can broadcast — no malicious SR/witness/peer role or off-chain compromise is needed. The only precondition is that the target `Exchange` pool's creator submits an `ExchangeInjectContract`, which is a normal, expected operation for anyone operating a `Exchange` market on java-tron.

### Recommendation
Add a caller-supplied minimum/maximum bound (mirroring `ExchangeTransactionContract.expected`) to `ExchangeInjectContract`, and validate the freshly computed `anotherTokenQuant` against it in `ExchangeInjectActuator.doValidate()`, rejecting the transaction if the pool ratio has moved unfavorably beyond the injector's tolerance.

### Proof of Concept
1. Attacker observes a pending `ExchangeInjectContract` from the exchange creator in the mempool (or simply races to land a transaction in the same block before it).
2. Attacker submits an `ExchangeTransactionContract` that swaps a large amount of `firstTokenId` for `secondTokenId` (or vice versa), shifting `firstTokenBalance`/`secondTokenBalance` sharply per `ExchangeCapsule.transaction()`.
3. The creator's `ExchangeInjectContract` executes against the now-skewed ratio in `ExchangeInjectActuator.execute()` (lines 71-83), causing the creator to deposit/receive tokens at the manipulated rate — with no `expected`-style check to abort.
4. Attacker submits a reversing `ExchangeTransactionContract` to restore the ratio and realize a profit extracted from the value the creator over-contributed, analogous to withdrawing collateral from the pool cheaply as in the referenced `buyCollateralFromAuction` report.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L215-227)
```java
    if (Arrays.equals(tokenID, firstTokenID)) {
      anotherTokenID = secondTokenID;
      anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
          .divide(bigFirstTokenBalance).longValueExact();
      newTokenBalance = addExact(firstTokenBalance, tokenQuant);
      newAnotherTokenBalance = addExact(secondTokenBalance, anotherTokenQuant);
    } else {
      anotherTokenID = firstTokenID;
      anotherTokenQuant = bigFirstTokenBalance.multiply(bigTokenQuant)
          .divide(bigSecondTokenBalance).longValueExact();
      newTokenBalance = addExact(secondTokenBalance, tokenQuant);
      newAnotherTokenBalance = addExact(firstTokenBalance, anotherTokenQuant);
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L229-236)
```java
    if (anotherTokenQuant <= 0) {
      throw new ContractValidateException("the calculated token quant  must be greater than 0");
    }

    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    if (newTokenBalance > balanceLimit || newAnotherTokenBalance > balanceLimit) {
      throw new ContractValidateException("token balance must less than " + balanceLimit);
    }
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L60-76)
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

```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L173-221)
```java
    byte[] tokenID = contract.getTokenId().toByteArray();
    long tokenQuant = contract.getQuant();
    long tokenExpected = contract.getExpected();

    if (dynamicStore.getAllowSameTokenName() == 1
        && !Arrays.equals(tokenID, TRX_SYMBOL_BYTES)
        && !isNumber(tokenID)) {
      throw new ContractValidateException("token id is not a valid number");
    }
    if (!Arrays.equals(tokenID, firstTokenID) && !Arrays.equals(tokenID, secondTokenID)) {
      throw new ContractValidateException("token is not in exchange");
    }

    if (tokenQuant <= 0) {
      throw new ContractValidateException("token quant must greater than zero");
    }

    if (tokenExpected <= 0) {
      throw new ContractValidateException("token expected must greater than zero");
    }

    if (firstTokenBalance == 0 || secondTokenBalance == 0) {
      throw new ContractValidateException("Token balance in exchange is equal with 0,"
          + "the exchange has been closed");
    }

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
