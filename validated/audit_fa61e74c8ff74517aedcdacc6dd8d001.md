### Title
Missing slippage/minimum-output protection in `ExchangeInjectActuator` allows sandwich-attack theft of liquidity provider funds - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java])

### Summary
`ExchangeInjectContract` lets the creator of a bancor-style TRC10/TRX `Exchange` pool inject additional liquidity of one token, with the paired amount of the other token (`anotherTokenQuant`) computed automatically from the current pool ratio at execution time. There is no user-supplied minimum/maximum bound on `anotherTokenQuant`, so a pool ratio manipulated immediately before the injection transaction is applied (a sandwich attack using `ExchangeTransactionContract` trades) can force the injector to pay far more of the second token than they intended, at the attacker's benefit.

### Finding Description
`ExchangeInjectContract` only carries `owner_address`, `exchange_id`, `token_id`, and `quant` — no expected/minimum amount field, unlike `ExchangeTransactionContract`, which explicitly carries an `expected` field used as slippage protection (`anotherTokenQuant < tokenExpected` check in `ExchangeTransactionActuator`). [1](#0-0) 

In `ExchangeInjectActuator.doValidate()` and `execute()`, the counterpart amount `anotherTokenQuant` is derived purely from the *current* on-chain pool ratio (`firstTokenBalance`/`secondTokenBalance`) at the moment validation/execution runs, with no ability for the caller to cap it: [2](#0-1) 

The same unconstrained ratio-derived computation is repeated in `execute()`: [3](#0-2) 

Only the exchange's creator (an address chosen at `ExchangeCreateContract` time, but reachable by any ordinary account that creates its own exchange pool) can call `ExchangeInject`: [4](#0-3) 

Because TRON's `Exchange` (bancor/AMM-style) pools allow any account to submit `ExchangeTransactionContract` trades against a given `exchange_id` at any time (the same class of operation implicated in the analog Sherlock report), an attacker can:
1. Front-run the victim's pending `ExchangeInjectContract` transaction with a large `ExchangeTransactionContract` trade that skews `firstTokenBalance`/`secondTokenBalance` heavily.
2. Let the victim's injection execute against the skewed ratio, forcing them to deposit a disproportionately large amount of the paired token (`anotherTokenQuant`) for the same `tokenQuant`.
3. Back-run with a reverse trade to restore the ratio and extract the resulting arbitrage profit, which comes directly out of the victim's injected balance — analogous to sandwiching Curve's `get_dy()`-based slippage check that mirrors the live execution formula, described in the referenced report.

This is architecturally the same root cause as the reported bug class: a value that should act as slippage protection (the amount the user actually risks paying/receiving) is computed live from the manipulable pool state at execution time, with no independently user-supplied bound.

### Impact Explanation
An exchange creator injecting liquidity can be forced, through transaction ordering (sandwiching) around block production, to overpay the paired token relative to the ratio they intended when they signed and broadcast the transaction. Funds transferred as the excess paired-token amount are permanently and irreversibly lost to the sandwiching account through the resulting arbitrage trades — a concrete unauthorized transfer of value/theft of funds from a single signed transaction, satisfying the Medium severity bar (loss of funds without any protocol-level protection or recourse).

### Likelihood Explanation
Likelihood is bounded by the precondition that a victim must be an exchange creator actively injecting liquidity into their own pool, and that the pool must have exploitable depth/latency for a sandwich. TRON block production is fast and deterministic (SR-controlled ordering), but transactions from the mempool can still be reordered/timed by the block-producing witness or by racing broadcast of trades immediately before/after observing a pending `ExchangeInjectContract` transaction, since witnesses/relayers see pending transactions before inclusion. This is a realistic, unprivileged, single-transaction-reachable path (`ExchangeTransactionContract` is open to any account), though the necessary role (being the pool's creator performing an inject) narrows the affected population compared to a fully public swap-based sandwich.

### Recommendation
Add a user-supplied bound to `ExchangeInjectContract` (e.g., `max_another_token_quant` or `min_another_token_quant`, mirroring the `expected` field already present on `ExchangeTransactionContract`), and enforce it in `ExchangeInjectActuator.doValidate()`/`execute()` by rejecting the transaction if the computed `anotherTokenQuant` exceeds/falls short of the caller-specified bound, exactly as done for `ExchangeTransactionContract`'s `tokenExpected` check.

### Proof of Concept
1. Account A creates an `Exchange` pool for TRX/TokenX via `ExchangeCreateContract`, becoming its `creatorAddress`.
2. Account A broadcasts an `ExchangeInjectContract` transaction injecting `tokenQuant` TRX, expecting `anotherTokenQuant` TokenX to be paired at the current ~1:R ratio.
3. Attacker B observes A's pending transaction and, before it is included, submits an `ExchangeTransactionContract` trade against the same `exchange_id` that sharply skews the TRX/TokenX ratio (e.g., dumping TokenX to inflate the TRX price in TokenX terms).
4. A's `ExchangeInjectContract` executes against the skewed ratio; `ExchangeInjectActuator.execute()` (lines 71-99) computes a much larger `anotherTokenQuant` than A anticipated and deducts it from A's TokenX balance, per `accountCapsule.reduceAssetAmountV2(anotherTokenID, anotherTokenQuant, ...)`.
5. B submits a reverse `ExchangeTransactionContract` trade restoring the ratio and extracting the arbitrage profit, realizing a net gain funded by A's over-deposited TokenX. [5](#0-4)

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L88-99)
```java
      if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, tokenQuant));
      } else {
        accountCapsule.reduceAssetAmountV2(tokenID, tokenQuant, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, anotherTokenQuant));
      } else {
        accountCapsule
            .reduceAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore);
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L175-177)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
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
