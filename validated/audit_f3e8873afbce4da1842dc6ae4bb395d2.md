### Title
Missing slippage control in `ExchangeInjectActuator` and `ExchangeWithdrawActuator` allows loss of funds - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
TRON's bancor-style `Exchange` contract lets an account inject or withdraw liquidity from a `first/second` token pool. Unlike `ExchangeTransactionActuator`, which correctly implements a user-supplied `expected` value to bound slippage, `ExchangeInjectContract` and `ExchangeWithdrawContract` compute the counterpart token amount (`anotherTokenQuant`) purely from the pool's on-chain balances **at execution time**, with no user-supplied minimum/maximum bound. Since the pool ratio can shift between the time a user signs/broadcasts the transaction and the time it is packed into a block (e.g., due to other users' `ExchangeTransactionContract` trades), the amount of the counterpart token pulled from (inject) or paid to (withdraw) the user can differ arbitrarily from what was expected off-chain, exactly the same root cause described in the Napier `Tranche.issue()` report where the minted amount depends on a scale that can change between simulation and execution.

### Finding Description
In `ExchangeInjectActuator.execute()`, the ratio-derived `anotherTokenQuant` is computed from the live `firstTokenBalance`/`secondTokenBalance` of the `ExchangeCapsule` and immediately deducted from the caller's balance/asset with no way for the caller to cap it: [1](#0-0) 

The validate step recomputes the same ratio for pre-flight checks, but it uses the state read during validation, not necessarily the state at execution, and there is no contract field for an expected/minimum bound (unlike `ExchangeTransactionContract.getExpected()`): [2](#0-1) 

Similarly, `ExchangeWithdrawActuator.execute()` computes `anotherTokenQuant` (the amount the user receives back) from the same live pool ratio with no floor/ceiling supplied by the caller: [3](#0-2) 

By contrast, `ExchangeTransactionActuator` demonstrates the protocol team already recognizes this exact bug class and mitigates it for trades via `tokenExpected`, validated against the just-computed `anotherTokenQuant`: [4](#0-3) 

Both `ExchangeInjectContract` and `ExchangeWithdrawContract` lack any equivalent field, so the actuators cannot enforce a caller-specified bound even if they wanted to protect the user. Because normal, unprivileged `ExchangeTransactionContract` trades from any account can move `firstTokenBalance`/`secondTokenBalance` between the time an inject/withdraw transaction is signed and the time it lands on-chain, the effective ratio used for `anotherTokenQuant` computation is attacker/market-influenced and not guaranteed to match what the exchange owner intended.

### Impact Explanation
- Inject: the exchange owner can be forced to pay far more of the counterpart token than intended when injecting liquidity, since `anotherTokenQuant` scales with whatever the pool ratio happens to be at execution — a direct, unbounded loss of the injected/paired assets.
- Withdraw: the exchange owner can receive far less of the counterpart token than expected when withdrawing liquidity, again due to ratio drift at execution time.
- This is asset loss for the actor operating the exchange (the exchange creator is a normal, unprivileged account — anyone can call `ExchangeCreateActuator`), reachable purely via ordinary signed transactions, matching the Medium-severity classification of the original report (loss of assets due to missing slippage protection).

### Likelihood Explanation
Any account can create an exchange and is then the sole party permitted to inject/withdraw (`ExchangeInjectActuator`/`ExchangeWithdrawActuator` both check `accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())`), so this is directly reachable by an ordinary transaction broadcaster. Ratio drift can be triggered incidentally by unrelated market activity (any account calling `ExchangeTransactionContract` on the same pool), or intentionally front-run by a third party observing the pending inject/withdraw transaction in the mempool, making exploitation straightforward on a live chain with active exchange trading.

### Recommendation
Add an `expected`/bound field to `ExchangeInjectContract` and `ExchangeWithdrawContract` (mirroring `ExchangeTransactionContract.getExpected()`), and enforce it in `ExchangeInjectActuator`/`ExchangeWithdrawActuator` validate/execute — e.g., reject the inject if the computed `anotherTokenQuant` exceeds a caller-supplied maximum, and reject the withdraw if the computed `anotherTokenQuant` is below a caller-supplied minimum.

### Proof of Concept
1. Account A creates an exchange pool for tokens X/Y via `ExchangeCreateActuator`.
2. Account A observes off-chain that injecting `tokenQuant` of X currently requires paying `anotherTokenQuant` of Y based on the current `firstTokenBalance`/`secondTokenBalance`, and broadcasts an `ExchangeInjectContract` transaction accordingly.
3. Before A's transaction is packed, another (unprivileged) account B broadcasts an `ExchangeTransactionContract` trade against the same pool, shifting `firstTokenBalance`/`secondTokenBalance` significantly.
4. When A's `ExchangeInjectContract` executes, `ExchangeInjectActuator.execute()` recomputes `anotherTokenQuant` from the now-shifted balances at [1](#0-0) , deducting a much larger amount of Y from A's account than A intended off-chain, with no field in `ExchangeInjectContract` to cap this. The same scenario applies symmetrically to `ExchangeWithdrawActuator`, where A ends up receiving less Y than expected.

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
