### Title
No Minimum/Maximum Bound on Computed Counterpart Amount in `ExchangeInjectActuator`/`ExchangeWithdrawActuator` Exposes Exchange Creators to Sandwich/Front-Running Losses - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`)

### Summary
The internal Bancor-style token exchange (`ExchangeInjectContract`/`ExchangeWithdrawContract`) computes the counterpart token amount (`anotherTokenQuant`) from the *current* on-chain pool ratio at execution time, but the contract protobuf provides no field for the caller to bound that computed amount. This is the same root-cause class as the reported issue (a swap operation with no slippage protection, so the amount charged/received depends on state that can shift between submission and execution), except here it is `ExchangeTransactionContract` (which *does* have an `expected` slippage-protection field, see line [1](#0-0) ) that got the fix, while the sibling liquidity actuators `ExchangeInjectActuator` and `ExchangeWithdrawActuator` never received an equivalent bound.

### Finding Description
`ExchangeInjectContract` only carries `owner_address`, `exchange_id`, `token_id`, and `quant` — there is no `expected`/`min`/`max` field: [2](#0-1) 

In `ExchangeInjectActuator.doValidate()` and `execute()`, `anotherTokenQuant` is derived purely from the exchange's live `firstTokenBalance`/`secondTokenBalance` ratio at the moment the transaction is processed, and the account is unconditionally debited that computed amount: [3](#0-2) 

The only checks performed are that `anotherTokenQuant > 0`, that the resulting pool balances stay under `getExchangeBalanceLimit()`, and that the caller has sufficient balance to pay whatever amount gets computed: [4](#0-3) 

None of these validations bound `anotherTokenQuant` to what the caller actually intended to pay when they built and signed the transaction. `ExchangeWithdrawActuator` has the identical pattern — `anotherTokenQuant` computed from the live pool state and paid out with no min/max check: [5](#0-4) 

By contrast, `ExchangeTransactionActuator` includes an `expected` field that acts as slippage protection, rejecting the trade if the pool has moved unfavorably: [1](#0-0) 

Because `ExchangeInjectContract`/`ExchangeWithdrawContract` transactions sit in the mempool with a fixed `token_id`/`quant`, an attacker (or any third party, since `ExchangeTransactionContract` is fully unprivileged) can submit a same-block `ExchangeTransactionContract` swap that shifts the pool's `firstTokenBalance`/`secondTokenBalance` ratio immediately before the pending inject/withdraw transaction executes. When the exchange creator's inject transaction is subsequently applied, `anotherTokenQuant` is recomputed against the manipulated ratio, so the creator can be forced to pay (inject) or receive (withdraw) a materially different amount of the counterpart token than intended when they signed the transaction — with no on-chain mechanism to reject the trade if the price moved beyond an acceptable bound.

### Impact Explanation
An exchange creator injecting liquidity can be sandwiched into contributing far more of the counterpart asset than intended (effectively donating value to the attacker who reverses their manipulating trade after the inject executes), or a withdrawal can return far less of the counterpart asset than expected. This is a direct, unauthorized loss of the creator's TRX/TRC-10 asset balance caused purely by missing slippage protection on a state-dependent calculation, which fits the accepted impact category of concrete theft/loss of funds via price manipulation.

### Likelihood Explanation
Exploitation only requires that the attacker observe a pending `ExchangeInjectContract`/`ExchangeWithdrawContract` transaction in the mempool and submit an `ExchangeTransactionContract` swap against the same `exchange_id` that lands in an earlier position within the same block (or a preceding block), both of which are ordinary, permissionless, signed transactions available to any account. No special SR/witness/committee privilege is needed — this is achievable by any actor who can craft and broadcast a transaction and predict/observe mempool contents, similar to the MEV/sandwich pattern described in the original report.

### Recommendation
Add a slippage-bound field (analogous to `expected` in `ExchangeTransactionContract`) to `ExchangeInjectContract` and `ExchangeWithdrawContract`, e.g. `expected_another_token_quant` with a tolerance, and validate in `ExchangeInjectActuator`/`ExchangeWithdrawActuator` that the computed `anotherTokenQuant` does not exceed (for inject) or fall below (for withdraw) the caller-specified bound before mutating balances.

### Proof of Concept
1. Exchange creator C signs and broadcasts `ExchangeInjectContract{exchange_id=X, token_id=A, quant=Q}` expecting to also pay `anotherTokenQuant` computed from the pool ratio at signing time.
2. Before C's transaction is packed into a block, attacker M submits `ExchangeTransactionContract{exchange_id=X, token_id=A, quant=large}` that shifts the `A:B` pool ratio unfavorably for C.
3. `ExchangeTransactionActuator.execute()` applies M's trade first, updating `firstTokenBalance`/`secondTokenBalance` via `Commons.putExchangeCapsule` (see `actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java` lines 61-96).
4. C's `ExchangeInjectActuator.execute()` then runs against the now-skewed ratio; `anotherTokenQuant` is recomputed at lines 71-83 of `ExchangeInjectActuator.java`, resulting in a materially larger debit of token B than C intended, with no reversion since there is no bound to check against.
5. M can subsequently reverse their trade to restore the ratio and extract the difference, completing a sandwich against C's liquidity injection.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** Tron protobuf protocol document.md (L1384-1401)
```markdown
     - message `ExchangeInjectContract`
    
       `owner_address`: address of owner.
    
       `exchange_id`: token pair id.
    
       `token_id`: token id to inject.
    
       `quant`: token amount to inject.
    
      ```java
      message ExchangeInjectContract {
          bytes owner_address = 1;
          int64 exchange_id = 2;
          bytes token_id = 3;
          int64 quant = 4;
      }
      ```
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L65-99)
```java
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

      long newBalance = subtractExact(accountCapsule.getBalance(), calcFee());
      accountCapsule.setBalance(newBalance);

      if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, tokenQuant));
      } else {
        accountCapsule.reduceAssetAmountV2(tokenID, tokenQuant, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, anotherTokenQuant));
      } else {
        accountCapsule
            .reduceAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore);
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L215-256)
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

    if (anotherTokenQuant <= 0) {
      throw new ContractValidateException("the calculated token quant  must be greater than 0");
    }

    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    if (newTokenBalance > balanceLimit || newAnotherTokenBalance > balanceLimit) {
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

    if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
      if (accountCapsule.getBalance() < addExact(anotherTokenQuant, calcFee())) {
        throw new ContractValidateException("balance is not enough");
      }
    } else {
      if (!accountCapsule.assetBalanceEnoughV2(anotherTokenID, anotherTokenQuant, dynamicStore)) {
        throw new ContractValidateException("another token balance is not enough");
      }
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
