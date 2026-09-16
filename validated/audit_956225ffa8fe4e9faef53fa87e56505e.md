### Title
Missing slippage protection in `ExchangeInjectActuator` liquidity provisioning enables sandwich attacks on exchange creators - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`)

### Summary
`ExchangeInjectContract`/`ExchangeInjectActuator` lets an exchange creator inject one token amount into a TRC10 exchange pool and computes the paired token amount (`anotherTokenQuant`) from the pool's *current* on-chain ratio at execution time, with no caller-supplied bound on that computed amount. Unlike `ExchangeTransactionContract`, which carries an `expected` minimum-output field that `ExchangeTransactionActuator` enforces, `ExchangeInjectContract` has no equivalent minimum/maximum parameter, so the transaction broadcaster (the exchange creator) has no way to bound the ratio they will actually be forced to inject at.

### Finding Description
`ExchangeInjectActuator.execute()` derives `anotherTokenQuant` purely from the live pool balances at execution time: [1](#0-0) 

`doValidate()` performs the identical ratio computation for validation but never compares it against any user-supplied bound — it only checks that the derived amount is `> 0` and that the account has sufficient balance: [2](#0-1) 

This is in stark contrast to `ExchangeTransactionActuator`, whose `ExchangeTransactionContract` includes an `expected` field explicitly designed to cap slippage, and which is enforced during validation: [3](#0-2) 

Because a pool's ratio can be moved arbitrarily within the same block by any account performing an `ExchangeTransactionContract` swap against that same exchange (no permission is required to submit a swap), an attacker can front-run the creator's pending `ExchangeInjectContract` with a large one-sided swap, forcing the creator's inject to be computed against a manipulated ratio, and then reverse the swap afterward (a classic sandwich pattern) to extract value from the creator's injected liquidity. `ExchangeInjectContract` only restricts the *caller* to be the exchange creator, but it does nothing to protect that caller's inject from execution-time price manipulation by any other network participant.

### Impact Explanation
The exchange creator (who is only required to be able to submit an `ExchangeCreateContract` transaction beforehand, i.e., any unprivileged account) can be forced to inject their token into the pool at an attacker-manipulated ratio, resulting in a genuine, unauthorized transfer of value/theft of funds from the creator to the sandwiching attacker via arbitrage extraction. This constitutes direct loss of funds for the affected account.

### Likelihood Explanation
Likelihood is Low-to-Medium: it requires the attacker to observe a pending `ExchangeInjectContract` in the mempool/block and to be included before and after it (sandwiching), which is achievable but requires timing/MEV capability similar to the referenced report. It's also limited to accounts that are exchange creators actively injecting liquidity, which is a narrower population than general users.

### Recommendation
Add a `min_another_token_quant` (and/or `max_another_token_quant`) bound to `ExchangeInjectContract`, and have `ExchangeInjectActuator.doValidate()`/`execute()` reject the transaction if the computed `anotherTokenQuant` falls outside the caller-specified bound, mirroring the `expected` slippage check already implemented in `ExchangeTransactionActuator`.

### Proof of Concept
1. Attacker monitors the mempool for the exchange creator's `ExchangeInjectContract` targeting exchange `E` (first/second token balances `B1`/`B2`).
2. Attacker submits an `ExchangeTransactionContract` swap that shifts `B1`/`B2` heavily in one direction, ordered immediately before the creator's inject in the same block.
3. `ExchangeInjectActuator.execute()` computes `anotherTokenQuant = floorDiv(secondTokenBalance * tokenQuant, firstTokenBalance)` using the manipulated `firstTokenBalance`/`secondTokenBalance`, forcing the creator to deposit a disadvantageous ratio (`actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java:73-82`).
4. Attacker submits a second `ExchangeTransactionContract` reversing the initial swap, restoring the ratio and extracting the value difference contributed by the creator's mispriced injection.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L215-246)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```
