### Title
ExchangeInjectActuator / ExchangeWithdrawActuator compute counter-token amounts from the live pool ratio with no minimum/maximum (slippage) bound, unlike ExchangeTransactionActuator - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
`ExchangeInjectActuator` and `ExchangeWithdrawActuator` derive the amount of the paired token to be transferred (`anotherTokenQuant`) purely from the exchange's on-chain balances at execution time, with no user-supplied expected/minimum/maximum bound. This is the same bug class as the reported `BalancedVault.claim()` issue: the actuator commits the transaction owner to an output amount that depends entirely on chain state that can change between transaction broadcast and block inclusion, with no slippage or deadline protection.

### Finding Description
In `ExchangeTransactionActuator` (the swap path), the protocol contract carries an explicit `expected` field, and `doValidate()` enforces `anotherTokenQuant >= tokenExpected` before the trade executes: [1](#0-0) 

By contrast, `ExchangeInjectActuator.execute()` computes the paired amount solely from the current `firstTokenBalance`/`secondTokenBalance` ratio, with no caller-supplied bound: [2](#0-1) 
and its validation path recomputes the very same ratio-derived value at validate time, again with no expected/limit check other than balance sufficiency: [3](#0-2) 

Similarly, `ExchangeWithdrawActuator.execute()` computes `anotherTokenQuant` from the live pool ratio with no expected/limit parameter: [4](#0-3) 
The only sanity check in `doValidate()` is a "not precise enough" tolerance check that itself is computed from the *current* balances at validation time, not from a caller-declared expectation, so it provides no protection against the ratio moving between the time the creator signs/broadcasts the transaction and the time it is actually executed on-chain: [5](#0-4) 

Any account (an unprivileged transaction broadcaster) can submit an `ExchangeTransactionContract` swap against the same exchange pair to shift `firstTokenBalance`/`secondTokenBalance` before the creator's inject/withdraw transaction is packed into a block, since `ExchangeTransactionActuator` has no permission restriction. This directly changes the ratio used by `ExchangeInjectActuator`/`ExchangeWithdrawActuator` at execution time.

### Impact Explanation
An exchange creator issuing `ExchangeInjectContract` or `ExchangeWithdrawContract` has no way to bound the counter-token amount they will receive or must pay. A third party can front-run the creator's inject/withdraw transaction with one or more `ExchangeTransactionContract` swaps to skew the pool ratio, causing:
- On withdraw: the creator receives a materially different (less favorable) split of the two tokens than expected when they signed the transaction, effectively transferring value to the attacker who can immediately reverse their swap after the withdraw executes (sandwich).
- On inject: the creator is forced to commit more of one asset than intended for the same nominal `tokenQuant`, or receives a worse LP-equivalent position.

This results in concrete loss of funds for the exchange creator due to unbounded slippage, satisfying "theft of funds" impact criteria for a Medium/High finding.

### Likelihood Explanation
Likelihood is moderate: it requires an attacker to monitor the mempool/pending transactions for `ExchangeInjectContract`/`ExchangeWithdrawContract` transactions and race a swap transaction ahead of them (sandwich attack), which is a standard, low-cost, and well-understood MEV technique achievable by any account since `ExchangeTransactionActuator` swaps are unrestricted and permissionless.

### Recommendation
Add an explicit slippage-bound field (e.g., `expectedAnotherTokenQuant` with min/max semantics analogous to `ExchangeTransactionContract.expected`) to `ExchangeInjectContract` and `ExchangeWithdrawContract`, and enforce it in `ExchangeInjectActuator.doValidate()`/`execute()` and `ExchangeWithdrawActuator.doValidate()`/`execute()` before mutating balances, mirroring the protection already present in `ExchangeTransactionActuator`.

### Proof of Concept
1. Exchange creator broadcasts an `ExchangeWithdrawContract` (or `ExchangeInjectContract`) transaction for exchange `E` with `tokenId = A`, `quant = Q`, expecting to receive/pay `anotherTokenQuant` computed from the current ratio `secondTokenBalance/firstTokenBalance` observed off-chain.
2. Attacker observes the pending transaction and broadcasts an `ExchangeTransactionContract` swap on the same exchange `E` that shifts `firstTokenBalance`/`secondTokenBalance`, ensuring it is included in the block before the creator's withdraw/inject transaction (standard front-run/sandwich).
3. When the creator's `ExchangeWithdrawActuator`/`ExchangeInjectActuator` executes (`ExchangeWithdrawActuator.java:74-89`, `ExchangeInjectActuator.java:71-83`), `anotherTokenQuant` is recomputed from the now-skewed balances, giving the creator a worse outcome than they intended, with no `expected`/bound check to reject the trade.
4. Attacker reverses their swap afterward, extracting the value difference from the creator's withdraw/inject, with no on-chain check preventing this because neither actuator has an `expected` protobuf field or bound validation like `ExchangeTransactionActuator` does.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L228-243)
```java
      if (allowHarden) {
        BigDecimal remainder = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance, 4, RoundingMode.HALF_UP)
            .subtract(BigDecimal.valueOf(anotherTokenQuant));
        if (remainder.compareTo(
            BigDecimal.valueOf(anotherTokenQuant).multiply(new BigDecimal("0.0001"))) > 0) {
          throw new ContractValidateException("Not precise enough");
        }
      } else {
        double remainder = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance, 4, BigDecimal.ROUND_HALF_UP).doubleValue()
            - anotherTokenQuant;
        if (remainder / anotherTokenQuant > 0.0001) {
          throw new ContractValidateException("Not precise enough");
        }
      }
```
