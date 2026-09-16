Confirmed. `ExchangeInjectContract` and `ExchangeWithdrawContract` only carry `token_id`/`quant`, with **no slippage/limit field**, unlike `ExchangeTransactionContract` which explicitly has an `expected` field for this exact purpose.### Title
No maxAnotherTokenQuant slippage protection in `ExchangeInjectActuator`/`ExchangeWithdrawActuator` allows front-run griefing of exchange creator's liquidity operations - (File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java)

### Summary
`ExchangeInjectContract` and `ExchangeWithdrawContract` only carry `owner_address`, `exchange_id`, `token_id`, and `quant` fields, with no user-supplied bound on the counterpart token amount that will be pulled from (inject) or paid to (withdraw) the caller. This is the same class of bug as the reported Arrakis `mint()` issue: the amount of the "other side" token is computed from live, externally-manipulable pool state at execution time, and the caller has no way to cap it.

### Finding Description
In `ExchangeInjectActuator.execute()`/`doValidate()`, when a creator injects `tokenQuant` of one token into a bancor-style exchange, `anotherTokenQuant` (the required amount of the other token that will simultaneously be deducted from the creator's balance) is derived purely from the pool's current `firstTokenBalance`/`secondTokenBalance` ratio: [1](#0-0) 

The `ExchangeInjectContract` protobuf has no field to bound `anotherTokenQuant`: [2](#0-1) 

Compare this to `ExchangeTransactionContract`, which explicitly carries an `expected` field used as a minimum-received guard, proving the codebase's designers recognized the need for such protection in the analogous swap path but omitted it for inject/withdraw: [3](#0-2) [4](#0-3) 

The pool ratio (`firstTokenBalance`/`secondTokenBalance`) can be shifted by any unprivileged account simply by broadcasting an `ExchangeTransactionContract` (a swap) against the same `exchange_id` before the creator's `ExchangeInjectContract`/`ExchangeWithdrawContract` transaction is packed into a block: [5](#0-4) 

An attacker can therefore front-run a pending inject transaction with a swap that skews the ratio, causing `anotherTokenQuant` computed at execution time to be far larger than what the creator anticipated when they built/signed the transaction — exactly the "no maxInput" scenario from the reported bug, except here it targets the exchange creator's inject/withdraw operations rather than a vault's mint. `ExchangeWithdrawActuator` has the identical structural issue (also no max/min bound on `anotherTokenQuant`), only mitigated by an unrelated 0.01% precision check, not a user-supplied bound: [6](#0-5) 

### Impact Explanation
The exchange creator can be forced to deposit (inject) more of the paired token than intended, or receive less than intended on withdraw, purely due to third-party front-running of an unrelated swap transaction. Because inject/withdraw are restricted to the exchange's creator account only, the attacker cannot steal funds outright, but can grief the creator into an economically unfavorable liquidity operation (excess token pulled from balance, or reduced withdrawal proceeds) with no way for the creator to protect themselves via a signed transaction parameter. This is a fund-loss/economic-manipulation issue affecting an unprivileged reachable code path (any account can submit the front-running `ExchangeTransactionContract`), qualifying as Medium severity, consistent with the reported issue's classification.

### Likelihood Explanation
`ExchangeTransactionContract` is callable by any account holding either token in the pair and requires no special privilege, making the front-running precondition trivially reachable by any transaction broadcaster. The creator's inject/withdraw transaction, once broadcast, sits in the mempool and its final on-chain amounts are only fixed at block-application/actuator-execution time, giving an attacker a normal front-running window (same MEV/ordering assumption underlying the original report).

### Recommendation
Add a `max_another_token_quant` field to `ExchangeInjectContract` and a `min_another_token_quant` (or `max`, depending on direction) field to `ExchangeWithdrawContract`, mirroring the `expected` field already present on `ExchangeTransactionContract`. In `ExchangeInjectActuator.doValidate()`/`execute()` and `ExchangeWithdrawActuator.doValidate()`/`execute()`, revert with a `ContractValidateException` if the computed `anotherTokenQuant` exceeds (inject) or falls below (withdraw) the caller-supplied bound.

### Proof of Concept
1. Exchange creator wants to inject `tokenQuant` of `firstTokenID` and expects `anotherTokenQuant` ≈ `secondTokenBalance * tokenQuant / firstTokenBalance` of `secondTokenID` to be deducted, based on the pool state they observed when building the transaction.
2. Creator broadcasts an `ExchangeInjectContract` transaction with this `tokenQuant`.
3. Before this transaction is included, an attacker (any account, unprivileged) broadcasts an `ExchangeTransactionContract` swap against the same `exchange_id`, shifting `firstTokenBalance`/`secondTokenBalance` significantly (e.g., buying up most of `secondTokenID` from the pool), per `ExchangeTransactionActuator.execute()` [5](#0-4) .
4. When the creator's inject transaction executes, `anotherTokenQuant` is recomputed against the new, skewed ratio in `ExchangeInjectActuator.doValidate()` [1](#0-0) , pulling a much larger amount of `secondTokenID` from the creator's balance than intended, with no contract-level parameter available to cap this since `ExchangeInjectContract` lacks any such field [7](#0-6) .

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

**File:** protocol/src/main/protos/core/contract/exchange_contract.proto (L17-29)
```text
message ExchangeInjectContract {
  bytes owner_address = 1;
  int64 exchange_id = 2;
  bytes token_id = 3;
  int64 quant = 4;
}

message ExchangeWithdrawContract {
  bytes owner_address = 1;
  int64 exchange_id = 2;
  bytes token_id = 3;
  int64 quant = 4;
}
```

**File:** protocol/src/main/protos/core/contract/exchange_contract.proto (L31-37)
```text
message ExchangeTransactionContract {
  bytes owner_address = 1;
  int64 exchange_id = 2;
  bytes token_id = 3;
  int64 quant = 4;
  int64 expected = 5;
}
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-69)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();

      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
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
