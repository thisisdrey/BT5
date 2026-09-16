### Title
Missing slippage protection in ExchangeInject/ExchangeWithdraw actuators allows fund loss from ratio drift - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java])

### Summary
The TRC10 bancor-style `Exchange` supports three operations: `ExchangeTransactionContract` (trade), `ExchangeInjectContract` (add liquidity) and `ExchangeWithdrawContract` (remove liquidity). Only the trade contract carries an `expected` field used as slippage protection [1](#0-0) . `ExchangeInjectContract` and `ExchangeWithdrawContract` have no equivalent minimum/maximum bound field, so the paired token amount is derived purely from the exchange's current pool ratio at execution time, exactly the same bug class as the reported Burve `islandLiqToShares` issue where a value depending on a mutable ratio (`sqrtRatioX96`) is used with no way for the caller to bound the outcome.

### Finding Description
For `ExchangeInjectActuator`, the caller specifies only `tokenQuant` of one side of the pair; the actuator computes `anotherTokenQuant` from the live pool balances and deducts it from the caller's account in the same execution [2](#0-1) . The validation step performs the identical ratio calculation right before execution but there is no parameter constraining how much `anotherTokenQuant` the sender is willing to pay [3](#0-2) .

For `ExchangeWithdrawActuator`, the caller specifies `tokenQuant` to withdraw and `anotherTokenQuant` (the amount returned) is likewise computed from the live pool ratio with no lower bound the caller controls [4](#0-3) . The protobuf definitions for these two messages carry only `owner_address`, `exchange_id`, `token_id`, and `quant` — no `expected` field — unlike `ExchangeTransactionContract` which explicitly adds `expected` for this purpose [5](#0-4) .

Between the moment a user signs an `ExchangeInjectContract`/`ExchangeWithdrawContract` transaction and the moment it is packed into a block, other transactions (e.g. `ExchangeTransactionContract` trades) can move `firstTokenBalance`/`secondTokenBalance`, shifting the ratio used in `anotherTokenQuant` computation. Because the actuator has no way to reject an unfavorable outcome, the caller can be forced to deposit more of the counter-asset than intended (inject) or receive less than intended (withdraw) — the exact "no slippage protection" bug class from the external report, just applied to TRON's native AMM-like exchange instead of Uniswap-V3/Island liquidity.

### Impact Explanation
An unprivileged account that is the exchange creator (the only actor permitted to call inject/withdraw, per `doValidate`'s creator check) can suffer direct loss of TRX/TRC10 funds when their liquidity operation executes at a worse ratio than what they observed when signing, since there is no on-chain enforceable bound. This is a concrete unauthorized-value-transfer/loss-of-funds condition reachable from a single signed transaction.

### Likelihood Explanation
Likelihood is medium: it requires ratio movement between signing and block inclusion, which can happen naturally due to trading activity on the same exchange pair, or be deliberately induced by a third party front-running the inject/withdraw with `ExchangeTransactionContract` trades to shift the ratio unfavorably before the victim's transaction executes.

### Recommendation
Add an `expected`-style bound to `ExchangeInjectContract` and `ExchangeWithdrawContract` (e.g., `max_another_quant` for inject and `min_another_quant` for withdraw), and enforce it in `ExchangeInjectActuator.doValidate()`/`execute()` and `ExchangeWithdrawActuator.doValidate()`/`execute()` analogous to the existing `tokenExpected` check in `ExchangeTransactionActuator` [1](#0-0) .

### Proof of Concept
1. Attacker/observer monitors the mempool for a pending `ExchangeInjectContract` (or `ExchangeWithdrawContract`) from a victim exchange creator with `tokenQuant = Q` on pair (A, B).
2. Attacker submits an `ExchangeTransactionContract` trade on the same `exchange_id` that shifts `firstTokenBalance`/`secondTokenBalance` unfavorably for the victim's pending operation, and ensures it lands in the same or an earlier block.
3. The victim's `ExchangeInjectActuator.execute()` (or `ExchangeWithdrawActuator.execute()`) then computes `anotherTokenQuant` from the now-shifted ratio [6](#0-5) , causing the victim to deposit more counter-tokens (inject) or receive fewer tokens (withdraw) than intended, with no contract-level parameter to reject this outcome.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
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

**File:** Tron protobuf protocol document.md (L1384-1440)
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
```
