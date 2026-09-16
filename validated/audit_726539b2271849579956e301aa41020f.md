## Title
Missing slippage protection in `ExchangeInjectActuator` and `ExchangeWithdrawActuator` allows front-running loss for exchange creators - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
java-tron's TRC10 bancor-style `Exchange` model has a direct analog to the reported `rebalanceAll` bug: `ExchangeInjectContract` (add liquidity) and `ExchangeWithdrawContract` (remove liquidity) compute the paired-token amount purely from the *current* on-chain pool balances at execution time, with no user-supplied minimum/maximum bound. Only `ExchangeTransactionContract` (the swap operation) carries an `expected` field that is checked against slippage.

### Finding Description
`ExchangeInjectActuator.execute` derives `anotherTokenQuant` from the live `firstTokenBalance`/`secondTokenBalance` ratio at execution time and moves exactly that amount from the creator's account, with no bound supplied by the transaction itself: [1](#0-0) . The same pattern exists in `doValidate` where `anotherTokenQuant` is (re)computed from the pool ratio with no min/max check against the value the creator expected when they signed the transaction: [2](#0-1) .

`ExchangeWithdrawActuator` has the identical issue — the amount of the paired token returned to the creator on withdrawal is derived from the current pool ratio with no minimum-output guard: [3](#0-2) .

By contrast, `ExchangeTransactionActuator` explicitly supports and enforces an `expected` (minimum output) field supplied by the caller: [4](#0-3) . This confirms the protocol designers recognize slippage protection is required for pool-ratio-dependent operations, but omitted it for inject/withdraw — exactly the same class of bug as the reported `rebalanceAll` issue where withdraw/deposit lacked `amount0Min`/`amount1Min` while the swap path had protection.

Because any unprivileged account can broadcast an `ExchangeTransactionContract` against the same exchange pool, an attacker can submit a large swap immediately before (or in the same block as) the exchange creator's pending `ExchangeInjectContract`/`ExchangeWithdrawContract` transaction to shift the pool ratio, then optionally reverse it afterward (classic sandwich). The creator's inject/withdraw is executed against the manipulated ratio with no way to bound the outcome.

### Impact Explanation
- On withdraw, the creator can receive a materially smaller amount of the paired token than the ratio implied when they signed the transaction — direct loss of funds.
- On inject, the creator can be forced to contribute a materially larger amount of the paired token than intended for the same `tokenQuant`, again a direct funds loss, since `anotherTokenQuant` is deducted from their account unconditionally: [5](#0-4) .
- This is a concrete unauthorized-value-transfer/loss-of-funds scenario reachable purely by broadcasting standard, already-supported transaction types (`ExchangeTransactionContract` sandwiching `ExchangeInjectContract`/`ExchangeWithdrawContract`) — no privileged role required.

### Likelihood Explanation
Likelihood is high in practice: any account can submit an `ExchangeTransactionContract` for a given `exchangeId`, and Exchange pool balances/ratios (including pending creator inject/withdraw transactions) are visible in the mempool, enabling straightforward front-running/sandwiching by any block producer or MEV-aware actor. The only precondition is that a legitimate creator issues an inject/withdraw against a pool with attacker-controlled liquidity/visibility, which is the normal operating mode of the Exchange feature.

### Recommendation
Add an `expected`/min-out (and optionally max-in) field to `ExchangeInjectContract` and `ExchangeWithdrawContract`, mirroring the protection already present in `ExchangeTransactionContract` (`tokenExpected` check in `ExchangeTransactionActuator.doValidate`, lines 217-221). `ExchangeInjectActuator.doValidate`/`execute` and `ExchangeWithdrawActuator.doValidate`/`execute` should reject the transaction if the computed `anotherTokenQuant` falls outside the caller-specified bound, exactly as the swap path currently does.

### Proof of Concept
1. Attacker observes a pending `ExchangeWithdrawContract` from the exchange creator (`ExchangeWithdrawActuator`) that will withdraw `tokenQuant` of `firstTokenID`, expecting to receive a proportional amount of `secondTokenID` per lines 77-89 of `ExchangeWithdrawActuator.java`.
2. Attacker submits (and gets included ahead of, e.g. via higher energy/priority or same-block ordering) an `ExchangeTransactionContract` swap that shifts `firstTokenBalance`/`secondTokenBalance` sharply in the attacker's favor.
3. The creator's `ExchangeWithdrawContract` then executes against the skewed ratio in `ExchangeWithdrawActuator.execute` (lines 77-89), returning a much smaller `anotherTokenQuant` than the ratio at signing time implied — since there is no `expected`/minimum check anywhere in `ExchangeWithdrawActuator`.
4. Attacker optionally reverses the swap afterward to restore the pool ratio, pocketing the difference extracted from the victim's withdrawal (or inject overpayment).

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L85-99)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L215-231)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L77-89)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```
