I have enough context now to write up the analysis.

### Title
Missing slippage protection in ExchangeInjectActuator allows LP front-running / value extraction - (File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java)

### Summary
`ExchangeInjectActuator` lets any account owning a TRC10-token/TRX exchange pool add liquidity by specifying only the token side and quantity it wants to inject. The paired ("another") token amount required to keep the pool ratio constant is computed **from the exchange's live balances at execution time**, and there is no field in `ExchangeInjectContract`/no validation step allowing the caller to bound that computed amount. This mirrors the Allo `fundPool` bug class: the depositor has no way to make their contribution conditional on the pool's actual state, so it can be front-run.

### Finding Description
`ExchangeInjectActuator.execute()` reads the exchange's current `firstTokenBalance`/`secondTokenBalance` and derives `anotherTokenQuant` purely from that live ratio: [1](#0-0) 

The same unconstrained computation is repeated in `doValidate()`: [2](#0-1) 

Unlike `ExchangeTransactionActuator`, which accepts a caller-supplied `expected` bound and rejects the trade if the computed output is worse than that bound: [3](#0-2) 

`ExchangeInjectContract` carries no analogous `expected`/`min`/`max` bound for the paired token quantity, and `ExchangeWithdrawActuator` at least enforces a "Not precise enough" ±0.01% ratio-precision check before paying out: [4](#0-3) 

`ExchangeInject` has none of this. An attacker who sees a pending `ExchangeInjectContract` transaction in the mempool can broadcast an `ExchangeTransactionContract` immediately before it (same block, higher energy/priority via fee or simply submitted first) that swaps a large amount into the pool, shifting `firstTokenBalance`/`secondTokenBalance` far from their expected ratio. When the victim's inject then executes, `anotherTokenQuant` is computed against the manipulated ratio, so the victim either:
- fails validation with "token balance is not enough" (their pre-computed budget for the paired token no longer matches what the actuator now demands), or
- succeeds but locks in a materially worse ratio than intended, effectively donating value to the pool at the manipulated price — which the attacker can then reclaim by reversing their swap right after the inject executes (classic sandwich), extracting value from the victim's liquidity contribution.

This is the same root cause identified in the Allo `fundPool` report: an unprivileged actuator/function computes a required contribution amount from live, attacker-influenceable state at execution time, with no way for the caller to bound or reject an unfavorable outcome.

### Impact Explanation
A liquidity provider funding an `Exchange` pool via `ExchangeInjectContract` can have the paired-asset amount they lock in manipulated by a front-running trader, resulting in an unfavorable/theft-adjacent value transfer from the injector to the attacker (loss of funds for the legitimate depositor), and/or unpredictable transaction failures. This is reachable by any unprivileged account broadcasting a standard `ExchangeTransactionContract`/`ExchangeInjectContract` pair of transactions — no special privileges are required (the injecting account must simply be the pool's `creatorAddress`, per `doValidate()`'s creator check, but the attacker driving the sandwich needs no privileges at all).

### Likelihood Explanation
Any actor can watch the (public) mempool for an `ExchangeInjectContract`, since `ExchangeCreateActuator`/`ExchangeInjectActuator` and their `Exchange`/`ExchangeV2` pools are a long-standing, still-supported feature. The trade needed to shift the ratio is a normal `ExchangeTransactionContract`, requiring only that the attacker hold either token. This makes the attack straightforward to execute whenever a sizable inject transaction is observed.

### Recommendation
Add a caller-specified bound (e.g., `min_another_token_quant` / `max_another_token_quant`, analogous to `expected` in `ExchangeTransactionContract`) to `ExchangeInjectContract`, and validate in `ExchangeInjectActuator.doValidate()`/`execute()` that the computed `anotherTokenQuant` falls within the caller's tolerance before mutating pool/account state, rejecting the transaction otherwise (similar in spirit to the existing "Not precise enough" check in `ExchangeWithdrawActuator`).

### Proof of Concept
1. Pool P has `firstTokenBalance = A`, `secondTokenBalance = B` (ratio A:B).
2. Victim broadcasts `ExchangeInjectContract(tokenId=first, quant=Q)` expecting to also deposit `Q*B/A` of the second token.
3. Attacker, seeing this in the mempool, broadcasts and gets included first an `ExchangeTransactionContract` that sells a large amount of the second token into P, sharply increasing `secondTokenBalance` relative to `firstTokenBalance` (new ratio A:B').
4. Victim's inject now executes against `Commons.getExchangeStoreFinal(...).get(...)` returning the *post-trade* pool state in `ExchangeInjectActuator.execute()` [5](#0-4) , computing `anotherTokenQuant = Q*B'/A`, which is far larger than the `Q*B/A` the victim budgeted for — either the tx reverts with "token balance is not enough", or (if the victim happens to hold enough) they lock in a much larger amount of the second token than the original fair ratio implied.
5. Attacker immediately submits a second `ExchangeTransactionContract` reversing the first trade, restoring the ratio and pocketing the value discrepancy created by the victim's mis-priced injection.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L57-83)
```java
      ExchangeCapsule exchangeCapsule;
      exchangeCapsule = Commons.getExchangeStoreFinal(dynamicStore, exchangeStore, exchangeV2Store)
          .get(ByteArray.fromLong(exchangeInjectContract.getExchangeId()));
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();
      long firstTokenBalance = exchangeCapsule.getFirstTokenBalance();
      long secondTokenBalance = exchangeCapsule.getSecondTokenBalance();

      byte[] tokenID = exchangeInjectContract.getTokenId().toByteArray();
      long tokenQuant = exchangeInjectContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant;

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
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
