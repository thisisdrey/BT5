### Title
Missing Slippage/Minimum-Output Protection on `ExchangeInjectContract` and `ExchangeWithdrawContract` Enables Sandwich Attacks on Bancor-style Exchange Liquidity Operations - (File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java, actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java)

### Summary
The `ExchangeInjectContract` and `ExchangeWithdrawContract` transactions, which let an exchange's creator add or remove liquidity from a TRC10 bancor-style token pair, compute the paired-token amount (`anotherTokenQuant`) purely from the *current* on-chain pool ratio at execution time and expose no user-supplied bound (no `minAnotherTokenQuant` / `maxAnotherTokenQuant`) to guard against that ratio changing between transaction submission and block inclusion.

### Finding Description
`ExchangeInjectActuator.execute()` derives the counter-token amount from the live pool balances and immediately debits/credits the account with no floor/ceiling check supplied by the caller: [1](#0-0) 

The same pattern occurs in `ExchangeWithdrawActuator.execute()`, where `anotherTokenQuant` is computed from the pool ratio at commit time and paid out without any minimum-amount parameter from the requester: [2](#0-1) 

The `doValidate()` methods for both actuators only bound-check exchange balance limits and, for withdraw, an internal rounding-precision tolerance (`"Not precise enough"`), never a caller-specified acceptable output range: [3](#0-2) 

By contrast, `ExchangeTransactionContract` (the swap/trade path) *does* carry an `expected` field enforced in validation (`"token required must greater than expected"`), which is exactly the EIP-4626-style slippage protection the external report says is missing: [4](#0-3) 

This asymmetry means the two operations that move liquidity into/out of the pool (analogous to a vault's `deposit()`/`withdraw()`) are unprotected while the pure-swap operation is protected. An attacker who can see a pending `ExchangeInjectContract`/`ExchangeWithdrawContract` (e.g. in the mempool) can submit an `ExchangeTransactionContract` immediately before it to shift `firstTokenBalance`/`secondTokenBalance`, causing the victim's inject/withdraw to execute at a manipulated ratio, and then reverse the swap afterward to restore the pool and capture the difference — a classic sandwich attack.

### Impact Explanation
Because the exchange creator has no way to bound the counter-token amount, a sandwich attacker can extract value directly from the exchange creator's injected or withdrawn assets, resulting in concrete loss of funds for that account. This matches the "theft of funds via unauthorized/unexpected value transfer" impact class, mirroring the vault report's core concern (users receiving materially worse conversion rates than expected with no recourse).

### Likelihood Explanation
Exploitation only requires an attacker to observe a pending transaction (via mempool/gRPC broadcast visibility) and race it with ordinary `ExchangeTransactionContract` calls before and after — no special privilege, SR/witness role, or protocol bug is needed, just normal transaction broadcasting capability available to any account. The precondition (being the exchange's creator) restricts who can be the *victim*, but the *attacker* is any unprivileged account, and pools with meaningful reserves are an attractive target.

### Recommendation
Add caller-supplied bound parameters to `ExchangeInjectContract` and `ExchangeWithdrawContract` (e.g. `expected`/`minAnotherTokenQuant` for withdraw, `maxAnotherTokenQuant` for inject), and enforce them in `ExchangeInjectActuator.doValidate()` / `ExchangeWithdrawActuator.doValidate()` the same way `tokenExpected` is enforced in `ExchangeTransactionActuator`, so a transaction reverts rather than executing at an unfavorable, manipulated ratio.

### Proof of Concept
1. Exchange creator broadcasts `ExchangeInjectContract` intending to add `tokenQuant` of token A, expecting to also lock in roughly `anotherTokenQuant` of token B based on the pool ratio observed off-chain.
2. Attacker observes this pending transaction and front-runs it with an `ExchangeTransactionContract` that sells a large amount of token A into the pool, shifting `firstTokenBalance`/`secondTokenBalance` sharply (validated only against `tokenExpected` set by the attacker itself, per `ExchangeTransactionActuator.doValidate()` lines 217-221).
3. The victim's `ExchangeInjectContract` now executes against the skewed ratio in `ExchangeInjectActuator.execute()` (lines 71-83), forcing the victim to lock in a far larger `anotherTokenQuant` of token B than intended, with no `doValidate()` check to reject it.
4. Attacker back-runs with a reverse `ExchangeTransactionContract` to restore the ratio and pocket the difference extracted from the victim's injected token B, completing the sandwich.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```
