## Title
Missing slippage protection in `ExchangeInjectActuator`/`ExchangeWithdrawActuator` enables front-running (sandwich) attacks against liquidity rebalancing - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java])

### Summary
Unlike `ExchangeTransactionActuator`, which requires callers to supply an `expected` minimum-output value and reverts if the AMM-computed output falls below it, the `ExchangeInjectContract` and `ExchangeWithdrawContract` paths compute the paired-token amount purely from the *current* on-chain pool ratio at execution time, with no caller-supplied bound and no slippage check.

### Finding Description
`ExchangeTransactionContract` includes an explicit `expected` field, and `ExchangeTransactionActuator.doValidate()` reverts with `"token required must greater than expected"` if the AMM output is worse than the trader's minimum: [1](#0-0) 

In contrast, `ExchangeInjectContract` only carries `token_id` and `quant` — no minimum/maximum bound for the paired token: [2](#0-1) 

`ExchangeInjectActuator.doValidate()` and `execute()` derive `anotherTokenQuant` strictly from the pool's balances read at execution time (`secondTokenBalance * tokenQuant / firstTokenBalance` or the inverse), and there is no comparison against any value chosen by the transaction sender: [3](#0-2)  The execution path repeats the same unguarded ratio computation: [4](#0-3) 

Because the pool ratio can be moved arbitrarily by any account submitting an `ExchangeTransactionContract` in the same block window (the exchange's Bancor-style ratio is fully attacker-controllable pre-confirmation via `ExchangeCapsule.transaction`), an attacker who observes a pending `ExchangeInjectContract`/`ExchangeWithdrawContract` in the mempool can:
1. Front-run with an `ExchangeTransactionContract` trade that skews `firstTokenBalance`/`secondTokenBalance`.
2. Let the victim's inject/withdraw execute at the manipulated ratio, forcing them to deposit/receive an unfavorable amount of the paired token (since `anotherTokenQuant` is recomputed against the *post-manipulation* balances with no floor/ceiling).
3. Back-run with an opposing trade to restore the ratio and pocket the difference.

This is exactly the class of issue in the referenced report: a rebalancing/liquidity operation recomputed against pool state at execution time with no slippage bound, letting an unprivileged mempool watcher extract value via front-run/back-run.

### Impact Explanation
An unprivileged attacker who merely watches the mempool and broadcasts ordinary `ExchangeTransactionContract`/`ExchangeInjectContract`/`ExchangeWithdrawContract` transactions can extract value from another account's liquidity-management transaction without their consent — a concrete theft of funds via unauthorized value extraction, reachable purely through signed transactions and the existing actuator/validate paths (`ExchangeInjectActuator`, `ExchangeWithdrawActuator`).

### Likelihood Explanation
The `expected` field pattern used in `ExchangeTransactionActuator` demonstrates the codebase's own developers recognized the need for slippage protection on this AMM, yet `ExchangeInjectContract`/`ExchangeWithdrawContract` were not given an equivalent bound. Any account can create an exchange (becoming its "creator") and any account can trade against it via `ExchangeTransactionContract`, so the preconditions for a sandwich attack (attacker able to trade before/after a pending inject/withdraw) are met by ordinary, permissionless transactions — no privileged role is required to execute the attack.

### Recommendation
Add a bound field (e.g., `expected_another_amount` or min/max) to `ExchangeInjectContract`/`ExchangeWithdrawContract`, and validate the AMM-derived `anotherTokenQuant` against it in `ExchangeInjectActuator.doValidate()`/`execute()` and `ExchangeWithdrawActuator`, mirroring the existing `expected` check in `ExchangeTransactionActuator.doValidate()`.

### Proof of Concept
1. Attacker observes victim's pending `ExchangeInjectContract` (inject `tokenQuant` of `firstTokenID`) in the mempool.
2. Attacker submits `ExchangeTransactionContract` selling a large amount of `secondTokenID` for `firstTokenID`, shifting `firstTokenBalance`/`secondTokenBalance` unfavorably for the pending inject.
3. Victim's `ExchangeInjectActuator.execute()` computes `anotherTokenQuant` from the now-skewed balances (lines 71-83), forcing the victim to contribute more `secondTokenID` than they would have at the pre-attack ratio, with no `expected`-style check to abort.
4. Attacker submits a reverse `ExchangeTransactionContract` to restore the ratio, realizing an arbitrage profit extracted from the victim's inject. [3](#0-2)

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
