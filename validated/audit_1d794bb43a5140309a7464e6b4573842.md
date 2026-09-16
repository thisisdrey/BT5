This confirms the analog exists. The `ExchangeCreateContract` is reachable by any funded account (it only requires token balances, no privileged role), so any unprivileged account can become an exchange "creator" and then invoke `ExchangeInjectContract`/`ExchangeWithdrawContract` on their own pool without any minimum-output/slippage-protection parameter, unlike `ExchangeTransactionContract` which explicitly has an `expected` field checked at [1](#0-0) .

### Title
Missing slippage protection in `ExchangeInjectContract`/`ExchangeWithdrawContract` allows sandwiching of TRC10 Bancor-exchange liquidity operations - (File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java, actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java)

### Summary
java-tron's on-chain Bancor-style TRC10 `Exchange` (a native, decentralized-exchange-like feature) supports `ExchangeCreateContract`, `ExchangeInjectContract`, `ExchangeWithdrawContract`, and `ExchangeTransactionContract`. Only `ExchangeTransactionContract` (the "swap" message) carries an `expected` minimum-output field enforced at validation time [2](#0-1) . The "provide liquidity" (`ExchangeInjectContract`) and "withdraw liquidity" (`ExchangeWithdrawContract`) messages have no such protection field at all, mirroring exactly the bug class described in the external report for AMM `ProvideLiquidity`/`WithdrawLiquidity`.

### Finding Description
`ExchangeInjectContract` only carries `owner_address`, `exchange_id`, `token_id`, and `quant` — no minimum-received parameter [3](#0-2) . When executed, `ExchangeInjectActuator` computes `anotherTokenQuant` (the amount of the paired token the account is forced to co-deposit) strictly from the exchange's reserves *at execution time*: `anotherTokenQuant = floorDiv(multiplyExact(secondTokenBalance, tokenQuant), firstTokenBalance)` [4](#0-3) . If the pool ratio shifts (e.g., via an interleaving `ExchangeTransactionContract` swap) between signing and execution/packing into a block, the account is forced to supply a different, unexpected amount of the paired asset, or the transaction can be crafted/timed adversarially so the depositor is forced into unfavorable co-deposit ratios.

`ExchangeWithdrawContract` has the identical structure — `owner_address`, `exchange_id`, `token_id`, `quant`, with no minimum-out field [5](#0-4) . `ExchangeWithdrawActuator` computes the pro-rata paired-token refund purely from reserves at execution time: `anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant).divide(bigFirstTokenBalance).longValueExact()` [6](#0-5) . Since the withdrawer specifies only the amount of one token they want out, and the actuator derives the companion amount at current (possibly manipulated) reserve ratio, there is no way to bound the outcome.

By contrast, `ExchangeTransactionActuator.doValidate()` explicitly checks `if (anotherTokenQuant < tokenExpected) { throw new ContractValidateException("token required must greater than expected"); }` [1](#0-0) , proving the codebase already recognizes and defends against this exact class of risk for swaps, but omitted equivalent bounds for inject/withdraw.

`ExchangeCreateContract` is reachable by any account holding TRC10 balances of both tokens (no committee/witness/permission gating beyond normal account and balance checks), so any unprivileged account can create its own exchange pool and then call inject/withdraw against it, or interact with any existing public exchange pool as long as it holds the tokens and (for inject) is the pool creator.

### Impact Explanation
An attacker can observe a pending `ExchangeInjectContract` or `ExchangeWithdrawContract` transaction in the mempool and front-run it with an `ExchangeTransactionContract` swap that shifts the reserve ratio, then optionally back-run to restore the pool, extracting value from the victim: a liquidity provider can be forced to deposit an unexpectedly large amount of the paired token relative to what they intended, and a withdrawer can receive a materially different mix of assets than expected (asset-composition slippage), which for the exchange creator (who is uniquely permitted to inject/withdraw per `!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())` checks in both actuators [7](#0-6) [8](#0-7) ) still results in a concrete loss of expected token value for that account. This is a fund-loss/value-extraction issue reachable purely via ordinary signed transactions, matching Medium severity in the source report's category.

### Likelihood Explanation
Likelihood is moderate: it requires the attacker to observe a specific pending inject/withdraw transaction and race it with a swap in the same or an adjacent block, which is feasible for any actor able to broadcast transactions and monitor the mempool, but requires an existing exchange pool with the creator actively injecting/withdrawing liquidity (a less frequent operation than swaps, and further limited since only the pool creator can inject/withdraw).

### Recommendation
Add explicit slippage-bound parameters to `ExchangeInjectContract` and `ExchangeWithdrawContract` (e.g., `another_token_min`/`another_token_max` for inject, `token_min`/`another_token_min` for withdraw), and enforce them in `ExchangeInjectActuator.doValidate()`/`execute()` and `ExchangeWithdrawActuator.doValidate()`/`execute()` analogous to the existing `expected` check in `ExchangeTransactionActuator`.

### Proof of Concept
1. Attacker observes victim's pending `ExchangeInjectContract` for exchange pool `X` depositing `tokenQuant` of `firstToken`.
2. Attacker submits an `ExchangeTransactionContract` swap on pool `X` that shifts `firstTokenBalance`/`secondTokenBalance` significantly, ordered before the victim's transaction in the same block (or a preceding block).
3. Victim's `ExchangeInjectContract` executes against the new, manipulated reserves in `ExchangeInjectActuator.execute()`, computing `anotherTokenQuant` from the shifted ratio [4](#0-3) , forcing the victim to co-deposit an unintended amount of the paired token, with no on-chain mechanism to reject the trade for being unfavorable. The analogous sequence with `ExchangeWithdrawContract` forces the victim to receive an unfavorable asset mix on withdrawal [6](#0-5) .

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L175-221)
```java
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

**File:** Tron protobuf protocol document.md (L1403-1420)
```markdown
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L175-177)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L181-183)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
    }
```
