### Title
`ExchangeInjectActuator`/`ExchangeWithdrawActuator` compute paired-token amounts from the manipulable spot balance ratio of the AMM pool, with no slippage protection - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`)

### Summary
`ExchangeInjectActuator` and `ExchangeWithdrawActuator` compute `anotherTokenQuant` (the amount of the paired token to be pulled from or returned to the caller) purely from the exchange pool's current `firstTokenBalance`/`secondTokenBalance` ratio — the on-chain equivalent of a Uniswap pool's spot price. Unlike `ExchangeTransactionContract` (the swap contract), which has a caller-supplied `expected` field enforced in validation to bound slippage, `ExchangeInjectContract` and `ExchangeWithdrawContract` have no such protection field. An attacker can shift the pool ratio with a preceding `ExchangeTransactionContract` swap (an ordinary, unprivileged operation) placed immediately before a victim's inject/withdraw transaction in the same block, causing the victim to deposit or receive an amount of the paired token far from what they intended — exactly analogous to the Panoptic `deployNewPool` spot-price liquidity-manipulation finding.

### Finding Description
In `ExchangeInjectActuator.doValidate()` and `execute()`, the amount of the "another" token required is derived directly from the live pool balances: [1](#0-0) 

The same pattern is used for withdrawals: [2](#0-1) 

These balances (`firstTokenBalance`/`secondTokenBalance`) represent the constant-product/bancor pool's current reserves — i.e., its spot price — and are trivially moved by any account issuing an ordinary `ExchangeTransactionContract` swap just before the inject/withdraw transaction lands, since `ExchangeCapsule.transaction()` mutates these balances on every swap: [3](#0-2) 

Critically, `ExchangeInjectContract` and `ExchangeWithdrawContract` carry only `owner_address`, `exchange_id`, `token_id`, and `quant` — no minimum/maximum bound on the derived `anotherTokenQuant`: [4](#0-3) 

By contrast, the swap contract `ExchangeTransactionContract` explicitly includes an `expected` field that is checked in `doValidate()`: [5](#0-4) 

This asymmetry mirrors the Panoptic report exactly: the swap path (`ExchangeTransactionActuator`, analogous to Panoptic's protected swaps) has slippage protection, while the liquidity-provision paths (`ExchangeInjectActuator`/`ExchangeWithdrawActuator`, analogous to `PanopticFactory.deployNewPool`) blindly trust the instantaneous pool ratio to size the counter-asset amount.

### Impact Explanation
A user calling `ExchangeInjectContract` (e.g., a pool creator adding liquidity, expecting to inject at the current market ratio) or `ExchangeWithdrawContract` can be forced into depositing/receiving an amount of the paired token dictated by an attacker-manipulated ratio. Because there is no `expected`/minimum bound, the victim has no on-chain way to cap the paired-token amount, and a large enough swap sandwiched around the victim's transaction can extract value from the victim's asset balance (their own funds are debited based on the skewed ratio), leading to direct loss of funds for the injecting/withdrawing account. This matches the "theft of funds via unbounded paired-asset amount" impact class validated for the original Panoptic finding.

### Likelihood Explanation
Any unprivileged account can broadcast an `ExchangeTransactionContract` swap and immediately follow it with a targeted victim's pending inject/withdraw within the same or an adjacent block (transactions execute sequentially in block order via `Manager`), requiring only sufficient assets to move the pool ratio — no special privileges, oracle collusion, or witness/committee cooperation needed. The `ExchangeBalanceLimit` check bounds absolute pool size but does not prevent ratio manipulation. This is a realistic, reachable path for any TRC10 exchange pool with moderate liquidity.

### Recommendation
Add a caller-supplied minimum/maximum bound (analogous to `expected` in `ExchangeTransactionContract`) to `ExchangeInjectContract` and `ExchangeWithdrawContract`, and enforce it in `ExchangeInjectActuator.doValidate()`/`ExchangeWithdrawActuator.doValidate()` against the computed `anotherTokenQuant`, rejecting the transaction if the derived amount falls outside the caller's tolerance.

### Proof of Concept
1. Attacker observes a pending `ExchangeInjectContract` from Victim on `exchangeId` X injecting `tokenQuant` of `firstTokenID`.
2. Attacker submits an `ExchangeTransactionContract` swap on the same exchange, large enough to shift `firstTokenBalance`/`secondTokenBalance` significantly (see `ExchangeCapsule.transaction()` mutating balances at [6](#0-5) ), ordered before Victim's transaction in the block.
3. Victim's `ExchangeInjectActuator.execute()` computes `anotherTokenQuant` from the now-skewed ratio at [7](#0-6) , causing Victim to inject/lose far more of the paired token than expected, with no `expected`-style check to reject the transaction.
4. Attacker can then reverse their initial swap to restore the pool ratio and extract the difference.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L71-83)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L74-89)
```java
      BigInteger bigFirstTokenBalance = new BigInteger(String.valueOf(firstTokenBalance));
      BigInteger bigSecondTokenBalance = new BigInteger(String.valueOf(secondTokenBalance));
      BigInteger bigTokenQuant = new BigInteger(String.valueOf(tokenQuant));
      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
        anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance).longValueExact();
        exchangeCapsule.setBalance(subtractExact(firstTokenBalance, tokenQuant),
            subtractExact(secondTokenBalance, anotherTokenQuant));
      } else {
        anotherTokenID = firstTokenID;
        anotherTokenQuant = bigFirstTokenBalance.multiply(bigTokenQuant)
            .divide(bigSecondTokenBalance).longValueExact();
        exchangeCapsule.setBalance(subtractExact(firstTokenBalance, anotherTokenQuant),
            subtractExact(secondTokenBalance, tokenQuant));
      }
```

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

**File:** Tron protobuf protocol document.md (L1394-1420)
```markdown
      ```java
      message ExchangeInjectContract {
          bytes owner_address = 1;
          int64 exchange_id = 2;
          bytes token_id = 3;
          int64 quant = 4;
      }
      ```
    
     - message `ExchangeWithdrawContract`
    
       `owner_address`: address of owner.
    
       `exchange_id`: token pair id.
    
       `token_id`: token id to withdraw.
    
       `quant`: token amount to withdraw.
    
      ```java
      message ExchangeWithdrawContract {
          bytes owner_address = 1;
          int64 exchange_id = 2;
          bytes token_id = 3;
          int64 quant = 4;
      }
      ```
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```
