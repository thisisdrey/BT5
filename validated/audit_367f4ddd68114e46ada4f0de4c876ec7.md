### Title
Front-runnable, unbounded exchange-ratio in `ExchangeInjectContract` leads to unexpected deposit amounts for liquidity providers - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`)

### Summary
`ExchangeInjectActuator` computes the amount of the "other" token an exchange creator must deposit strictly from the pool's *current* on-chain ratio at execution time, with no user-supplied bound on that computed amount. Unlike `ExchangeTransactionActuator`, which accepts and enforces a `getExpected()` slippage-limit field, `ExchangeInjectContract`/`ExchangeWithdrawContract` provide no analogous "hard limit" parameter. A third party can submit an `ExchangeTransactionContract` (swap) that executes immediately before the injector's transaction and shifts the pool ratio, causing the injector's actuator-computed `anotherTokenQuant` to differ materially from what they intended when they signed the transaction — exactly the "price is unpredictable / add a hard limit parameter" issue described in the report.

### Finding Description
In `ExchangeInjectActuator.execute()` and `doValidate()`, the amount of the counter-token required is derived purely from the exchange's live balances at the moment of execution: [1](#0-0) 

The same unconstrained ratio computation is repeated in validation: [2](#0-1) 

There is no `expected`/limit field in the contract and no check comparing the computed `anotherTokenQuant` against any caller-supplied bound — the only checks are `tokenQuant <= 0`, `anotherTokenQuant <= 0`, and the global `balanceLimit`. Contrast this with `ExchangeTransactionActuator`, which explicitly protects the swapper via a caller-supplied `tokenExpected` field that is checked against the actuator's computed output: [3](#0-2) 

Because Tron transactions are broadcast to the network and sit in the mempool/pending pool before being packed into a block, any unprivileged account can observe a pending `ExchangeInjectContract` transaction (which reveals `exchangeId` and `tokenQuant`) and race it with its own `ExchangeTransactionContract` swap against the same `exchangeId`, shifting `firstTokenBalance`/`secondTokenBalance` before the inject transaction executes. When the inject transaction is finally applied, `anotherTokenQuant` is recomputed from the new, attacker-influenced ratio — not the ratio the signer observed when constructing and signing the transaction.

### Impact Explanation
The exchange creator ends up depositing (and having their account debited via `reduceAssetAmountV2`/`setBalance`) a different amount of the counter-token than they intended, potentially at an economically unfavorable ratio manipulated by the front-runner. This is a direct, unbounded loss-of-funds vector for the liquidity provider with no way to cap their exposure, since the contract exposes no equivalent to `tokenExpected`. This is a genuine economic/fund-loss issue reachable by any account that can observe pending transactions and submit a competing `ExchangeTransactionContract`, matching the "Medium" bar for concrete loss of funds via an unprivileged, reachable path.

### Likelihood Explanation
Likelihood is limited by the precondition that the caller must be the exchange's creator (`accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())`) — an ordinary, unprivileged role obtained simply by creating the exchange via `ExchangeCreateContract`, not a system-privileged role. Any account can watch the mempool for a pending `ExchangeInjectContract` (or simply race an injection they anticipate) and submit a swap against the same `exchangeId` to shift the ratio before the inject executes. This requires no special access, only normal transaction broadcasting and awareness of pending transactions/timing — a realistic front-running scenario on any public blockchain.

### Recommendation
Add an `expected`/limit field to `ExchangeInjectContract` (and similarly to `ExchangeWithdrawContract`) analogous to `ExchangeTransactionContract.expected`, and validate in `ExchangeInjectActuator.doValidate()`/`execute()` that the computed `anotherTokenQuant` does not exceed (for inject) or fall below (for withdraw) the caller-specified bound, rejecting the transaction with `ContractValidateException` otherwise.

### Proof of Concept
1. Exchange creator A holds exchange `id=1` with balances `firstTokenBalance=1_000_000`, `secondTokenBalance=1_000_000` (1:1 ratio) and builds/broadcasts `ExchangeInjectContract{exchangeId=1, tokenId=first, quant=100_000}`, expecting to also deposit ~`100_000` of the second token.
2. Before A's transaction is packed, attacker B observes it in the mempool and broadcasts `ExchangeTransactionContract{exchangeId=1, tokenId=first, quant=large_X}` which is confirmed first, drastically shifting the pool ratio (e.g., to 1:4) via `ExchangeCapsule.transaction()` inside `ExchangeTransactionActuator.execute()` [4](#0-3) .
3. A's `ExchangeInjectContract` then executes against the new ratio; `anotherTokenQuant = floorDiv(multiplyExact(secondTokenBalance, tokenQuant), firstTokenBalance)` [5](#0-4)  now yields a value roughly 4x A's expectation, and this amount is unconditionally deducted from A's account with no `expected`-style check to abort the transaction, causing A to lose value relative to what was signed for.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L60-83)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L209-231)
```java
    BigInteger bigFirstTokenBalance = new BigInteger(String.valueOf(firstTokenBalance));
    BigInteger bigSecondTokenBalance = new BigInteger(String.valueOf(secondTokenBalance));
    BigInteger bigTokenQuant = new BigInteger(String.valueOf(tokenQuant));
    long newTokenBalance;
    long newAnotherTokenBalance;

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-76)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();

      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());

      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
      } else {
        anotherTokenID = firstTokenID;
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
