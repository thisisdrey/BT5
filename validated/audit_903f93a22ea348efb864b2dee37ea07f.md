## Analysis

I examined java-tron's on-chain "Exchange" (bancor-style AMM for TRC10 pairs) actuators, which are the closest analog to `spot_lp`'s liquidity operations, reachable directly by any unprivileged transaction broadcaster.

`ExchangeTransactionActuator` (a swap, not liquidity add/remove) already implements the exact slippage-protection pattern recommended in the report: it takes a user-supplied `expected` field and validates `anotherTokenQuant < tokenExpected` before executing. [1](#0-0) 

However, the two actuators that actually perform liquidity add/remove — the direct analog of `spot_lp`'s add/remove-liquidity code path — have **no such protection at all**.

### `ExchangeInjectActuator` (add liquidity)
It computes the paired token amount purely from the pool's current ratio at execution time, with no user-specified minimum: [2](#0-1) 
The only bound checked in validation is `anotherTokenQuant <= 0` and an overall exchange balance limit — not a slippage/minimum-received guard tied to what the user expected when they signed the transaction. [3](#0-2) 

### `ExchangeWithdrawActuator` (remove liquidity)
Same pattern: `anotherTokenQuant` is derived from `firstTokenBalance`/`secondTokenBalance` at execution time with no minimum-output parameter in `ExchangeWithdrawContract` at all. [4](#0-3) 

The protobuf messages confirm `ExchangeInjectContract` and `ExchangeWithdrawContract` only carry `owner_address`, `exchange_id`, `token_id`, `quant` — no `expected`/minimum field, unlike `ExchangeTransactionContract` which does carry `expected`. [5](#0-4) 

---

### Title
Missing slippage protection in `ExchangeInjectContract`/`ExchangeWithdrawContract` (AMM liquidity add/remove) - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
`ExchangeInjectActuator` and `ExchangeWithdrawActuator`, which implement liquidity add/remove for java-tron's on-chain bancor-style TRC10 exchange pools, compute the counterpart token amount from the pool's live `firstTokenBalance`/`secondTokenBalance` ratio strictly at execution time. Neither the contract messages (`ExchangeInjectContract`, `ExchangeWithdrawContract`) nor the actuator validation logic allow the caller to specify a minimum acceptable counterpart amount, unlike the sibling `ExchangeTransactionContract` (swap) which does carry an `expected` field and enforces it.

### Finding Description
When a user submits an `ExchangeInjectContract` or `ExchangeWithdrawContract` transaction, they only specify `token_id` and `quant`. The proportional counterpart amount (`anotherTokenQuant`) is calculated inside `execute()`/`doValidate()` using the pool balances read from the `ExchangeCapsule` at the moment the transaction actually executes on-chain — not at the moment the user signed it: [2](#0-1) [6](#0-5) 

Because `ExchangeTransactionContract` swaps (broadcastable by any account) mutate `firstTokenBalance`/`secondTokenBalance` via `exchangeCapsule.transaction(...)` and `Commons.putExchangeCapsule(...)`, any transaction ordered before a pending inject/withdraw within the same block (or across blocks while the inject/withdraw sits in mempool) changes the ratio used to compute the counterpart amount. The user has no way to bound the outcome, since neither the contract schema nor validation exposes a minimum/expected value for this path — contrasting directly with `ExchangeTransactionActuator`'s explicit `tokenExpected` check.

### Impact Explanation
An attacker (any unprivileged account, or a block producer/validator with ordering influence) can submit an `ExchangeTransactionContract` swap immediately before a victim's pending `ExchangeInjectContract`/`ExchangeWithdrawContract`, shifting the pool ratio unfavorably for the victim. The victim then injects assets at a worse ratio than intended (donating value to the pool/other LPs) or withdraws less of the counterpart token than expected, resulting in a measurable, unauthorized transfer of value away from the victim's account — a concrete funds-loss condition for an unprivileged, ordinary API-reachable operation.

### Likelihood Explanation
Both `ExchangeInjectContract` and `ExchangeWithdrawContract` are ordinary broadcastable transactions requiring only a valid signature and sufficient balance/asset holdings — reachable by any anonymous API client via the standard wallet contract-creation and broadcast RPC path. No special privilege, malicious SR/witness collusion, or network-level attack is required; a simple same-block front-run swap suffices, making this practically exploitable whenever exchange pools hold non-trivial liquidity.

### Recommendation
Add a user-specified minimum-acceptable-counterpart-amount field to `ExchangeInjectContract` and `ExchangeWithdrawContract` (analogous to `expected` in `ExchangeTransactionContract`), and enforce it in `ExchangeInjectActuator.doValidate()`/`ExchangeWithdrawActuator.doValidate()` by rejecting the transaction if the computed `anotherTokenQuant` falls short of (for inject) or below (for withdraw) the caller's specified bound.

### Proof of Concept
1. Pool P holds TRX/TokenA with balances such that the ratio favors a specific exchange rate.
2. Victim signs and broadcasts `ExchangeInjectContract` intending to inject at the current rate, expecting a specific `anotherTokenQuant` return based on the ratio observed off-chain.
3. Attacker broadcasts an `ExchangeTransactionContract` swap against the same pool, which executes first (same block, favorable ordering) and shifts `firstTokenBalance`/`secondTokenBalance` significantly.
4. Victim's `ExchangeInjectActuator.execute()` then computes `anotherTokenQuant` from the now-skewed ratio at [7](#0-6) , causing the victim to deposit assets at an unfavorable ratio with no on-chain check to prevent or bound the loss.

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

**File:** Tron protobuf protocol document.md (L1384-1442)
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
      }
      ```
```
