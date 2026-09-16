### Title
ExchangeInjectActuator computes the counterpart deposit amount from live pool reserves with no caller-supplied slippage bound - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java])

### Summary
`ExchangeInjectContract` only lets the caller fix the quantity of one token to inject (`quant`); the amount of the *other* token that is pulled from the account (`anotherTokenQuant`) is derived at validate/execute time from the current bonding-curve reserve ratio, with no minimum/maximum bound the caller can enforce. This is the same bug class as the reported Uniswap V3 `_increaseLiquidity()` finding (add-liquidity calls with `amount0Min`/`amount1Min` left at 0), reachable here by any account that creates a TRX/TRC-10 exchange pair and calls `ExchangeInjectContract`.

### Finding Description
`ExchangeInjectContract` is defined with only `owner_address`, `exchange_id`, `token_id`, and `quant` [1](#0-0)  — there is no `expected`/`min`/`max` field like the one that exists on `ExchangeTransactionContract` (`expected`, checked against `anotherTokenQuant` in `doValidate()`) [2](#0-1) [3](#0-2) .

In `ExchangeInjectActuator.doValidate()`, `anotherTokenQuant` is computed purely from the exchange's current `firstTokenBalance`/`secondTokenBalance` ratio at the moment the transaction is processed, and is only checked to be `> 0` and within the global balance limit — never bounded by anything the caller specified: [4](#0-3) 

The same unshielded computation is repeated in `execute()`, which deducts `tokenQuant` of the chosen token and `anotherTokenQuant` (recomputed against whatever the pool ratio is at execution time) of the counterpart token from the caller's account: [5](#0-4) 

Because the pool ratio (`firstTokenBalance`/`secondTokenBalance`) can be moved between the time the injector signs/broadcasts the transaction and the time it is actually applied — e.g. by any other account submitting an `ExchangeTransactionContract` trade against the same `exchange_id` that lands first in the same or an earlier block — the actual `anotherTokenQuant` charged to the injector can be arbitrarily larger than what the ratio looked like when they built the transaction. There is no field on the contract letting the injector cap this, unlike the swap path which has `expected`.

### Impact Explanation
An attacker monitoring the mempool for a pending `ExchangeInjectContract` from a known exchange creator can front-run it with an `ExchangeTransactionContract` that skews the reserve ratio, forcing the victim's inject call to compute and withdraw a much larger `anotherTokenQuant` than the victim intended (since the amount is derived from the live, attacker-manipulated ratio at execution time, not the ratio at signing time), then trade back afterward to restore the ratio and extract the difference — a classic sandwich attack causing direct loss of the injector's funds. This satisfies "theft of funds" under an unprivileged, ordinary account-vs-account interaction (the only requirement to call `ExchangeInjectContract` is being the exchange's creator, which is not a privileged/SR-type role).

### Likelihood Explanation
Any account can create an exchange (becoming its "creator") and later needs to inject liquidity; any other account can observe pending transactions and race a trade against the same `exchange_id` before the inject transaction is processed. No special privileges, keys, or SR/witness collusion are required — only normal transaction broadcasting capability, matching the allowed threat model (unprivileged transaction broadcaster).

### Recommendation
Add a caller-specified bound (e.g. `min_another_token_quant` / `max_another_token_quant`) to `ExchangeInjectContract`, and enforce it in `ExchangeInjectActuator.doValidate()`/`execute()` analogous to the `expected` check already present in `ExchangeTransactionActuator`, so the injector can reject execution if the computed counterpart amount falls outside their tolerance.

### Proof of Concept
1. Account C creates an exchange pair (TRX/TokenA) and is its "creator".
2. C builds `ExchangeInjectContract{exchange_id, token_id=TRX, quant=100 TRX}` expecting to also deposit ~100 TokenA at the current 1:1 ratio, and broadcasts it.
3. Attacker A observes this pending transaction and quickly submits `ExchangeTransactionContract` trades against the same `exchange_id` that shift the reserve ratio to e.g. 1:5 before C's inject transaction is applied.
4. When C's `ExchangeInjectContract` executes, `anotherTokenQuant` is recomputed against the now-skewed ratio (see `ExchangeInjectActuator.execute()` lines 71-83/`doValidate()` lines 215-227), causing C to deposit 500 TokenA instead of the ~100 they expected — with no way to have capped this since `ExchangeInjectContract` has no min/max field.
5. Attacker A then trades back, restoring the ratio and pocketing the value difference extracted from C's over-deposit.

### Citations

**File:** Tron protobuf protocol document.md (L1394-1401)
```markdown
      ```java
      message ExchangeInjectContract {
          bytes owner_address = 1;
          int64 exchange_id = 2;
          bytes token_id = 3;
          int64 quant = 4;
      }
      ```
```

**File:** Tron protobuf protocol document.md (L1422-1441)
```markdown
     - message `ExchangeTransactionContract`
    
       `owner_address`: address of owner.
    
       `exchange_id`: token pair id.
    
       `token_id`: token id to sell.
    
       `quant`: token amount to sell.
    
       `expected`: expected minimum number of tokens.
    
      ```java
      message ExchangeTransactionContract {
          bytes owner_address = 1;
          int64 exchange_id = 2;
          bytes token_id = 3;
          int64 quant = 4;
          int64 expected = 5;
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L215-236)
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
```
