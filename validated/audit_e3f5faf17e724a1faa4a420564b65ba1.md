## Finding

### Title
Missing slippage protection in `ExchangeInjectActuator` allows sandwich attacks on TRC10 Bancor-exchange liquidity injections - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`)

### Summary
`ExchangeInjectActuator` (the `ExchangeInjectContract` handler) computes the counter-token amount for a liquidity injection purely from the *current* on-chain token-balance ratio of the target `Exchange`, with no user-supplied bound on how much of the counter-token the injector is willing to spend/receive. An unprivileged trader can manipulate that ratio immediately before the injection transaction executes (via `ExchangeTransactionContract`) and reverse the manipulation afterward, extracting value from the exchange creator's injection — the exact "sandwich" pattern described in the external report for Lockbox V2's `deposit` instruction.

### Finding Description
When injecting liquidity, the actuator derives `anotherTokenQuant` from the live pool balances at execution time: [1](#0-0) 

The same unbounded calculation is repeated in validation: [2](#0-1) 

Unlike `ExchangeTransactionActuator`, which requires callers to supply a `tokenExpected` minimum-output bound that is checked against the on-chain-computed result before execution: [3](#0-2) 

`ExchangeInjectContract` has no analogous parameter — no `min`/`max` bound on `anotherTokenQuant` is validated against the caller's intent. The only sanity checks are that the derived amount is positive and within global balance limits: [4](#0-3) 

This mirrors the Lockbox V2 bug class precisely: a value derived from a manipulable current price (here, the TRC10 Bancor-exchange's `firstTokenBalance`/`secondTokenBalance` ratio, there Orca's `sqrt_price`) is used directly to compute how much of a second asset is required/paid, with no slippage bound supplied and checked against the caller's actual intent.

### Impact Explanation
An unprivileged trader (anyone able to broadcast `ExchangeTransactionContract`) can sandwich a pending `ExchangeInjectContract` transaction:
1. Front-run: submit an `ExchangeTransactionContract` trade that skews `firstTokenBalance`/`secondTokenBalance` in the target exchange (this only requires holding tradable balance in that exchange's tokens, no special privilege — see `ExchangeTransactionActuator.execute`).
2. The victim's `ExchangeInjectContract` executes against the skewed ratio, forcing them to supply a distorted `anotherTokenQuant` (more of the counter-token than they would at the fair price), since the exchange creator has no way to bound this amount.
3. Back-run: reverse the initial trade to restore the ratio, realizing a profit by having extracted assets injected at an unfavorable, manipulated ratio.

This causes direct, unauthorized loss of funds for the exchange creator (the `Exchange`'s `creatorAddress`, checked at `ExchangeInjectActuator.java:175`) and a corresponding unbacked/asymmetric profit for the attacker, satisfying "theft of funds" impact criteria.

### Likelihood Explanation
The attack requires only: (a) knowledge of a pending `ExchangeInjectContract` (visible in mempool/tx broadcast), and (b) the ability to submit ordinary `ExchangeTransactionContract` trades against the same exchange, which is available to any account with balance in either token — no committee, SR, or other elevated privilege is needed. Given java-tron's transaction ordering is influenced by block producers and fee/priority rather than strict FIFO with private mempool protection, sandwiching a visible pending transaction within the same block/producer window is feasible, matching the "medium" severity classification given to the analogous Lockbox V2 finding.

### Recommendation
Add a slippage-bound parameter to `ExchangeInjectContract` analogous to `ExchangeTransactionContract`'s `tokenExpected` — e.g., a `maxAnotherTokenQuant` (or `minAnotherTokenQuant`, depending on the token side) supplied by the caller — and validate the on-chain-computed `anotherTokenQuant` against that bound in `ExchangeInjectActuator.doValidate` before allowing execution, exactly mirroring the pattern already used in `ExchangeTransactionActuator`.

### Proof of Concept
1. Exchange E holds `firstTokenBalance = A`, `secondTokenBalance = B` for tokens `T1`/`T2`.
2. Exchange creator C broadcasts `ExchangeInjectContract(exchangeId=E, tokenId=T1, quant=Q)`, expecting `anotherTokenQuant ≈ B*Q/A` at current fair-market state.
3. Attacker M observes C's pending transaction and broadcasts `ExchangeTransactionContract(exchangeId=E, tokenId=T2, quant=large)` just before it, shifting `secondTokenBalance` upward relative to `firstTokenBalance` (per `ExchangeCapsule.transaction`, `ExchangeCapsule.java:124-169`).
4. C's inject transaction executes against the new skewed ratio, computing a materially different (larger) `anotherTokenQuant` than C intended, per `ExchangeInjectActuator.java:71-83`, with no bound check to reject it.
5. M submits a reverse `ExchangeTransactionContract` to restore the ratio, extracting the excess value effectively donated by C's injection at the manipulated ratio.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```
