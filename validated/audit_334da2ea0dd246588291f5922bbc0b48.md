### Title
Zero-balance "exchange closed" check reused in `ExchangeWithdrawActuator` permanently freezes the creator's remaining pool funds - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java])

### Summary
`ExchangeWithdrawActuator.doValidate()` reuses the same "Token balance in exchange is equal with 0, the exchange has been closed" guard that `ExchangeTransactionActuator` and `ExchangeInjectActuator` use to stop *trading* once one side of an AMM-style exchange pool is drained. However, this same check also blocks the pool creator's *withdrawal* path, so once one token side of the pool reaches zero, the creator can never withdraw the funds still sitting on the other, non-zero side. This is structurally identical to the Derby report: a state-transition condition ("pool closed" / "vault off") that is supposed to gate one class of operation (trading/allocation) is instead reused to gate withdrawal, permanently locking funds that legitimately belong to a user.

### Finding Description
Exchange pools in java-tron are single-owner AMM-like pairs created via `ExchangeCreateActuator`; only the creator can `ExchangeInject`/`ExchangeWithdraw` (validated at [1](#0-0) 
). Other users can trade against the pool via `ExchangeTransactionActuator`.

All three exchange actuators — `ExchangeTransactionActuator`, `ExchangeInjectActuator`, and `ExchangeWithdrawActuator` — share the identical guard: [2](#0-1) 
This same "closed" condition appears verbatim in `ExchangeTransactionActuator` [3](#0-2) 
and `ExchangeInjectActuator` [4](#0-3) 
.

The intent of this check for `ExchangeTransactionActuator`/`ExchangeInjectActuator` is reasonable: once a pool side is fully drained, price computation (division by a zero reserve) is undefined, so trading/injecting must stop. But `ExchangeWithdrawActuator` reuses the exact same predicate. Once `firstTokenBalance == 0` or `secondTokenBalance == 0` for any reason, the creator is permanently blocked from calling `ExchangeWithdraw` to reclaim the non-zero remaining balance on the other side, because `doValidate()` throws before any withdrawal math or balance mutation happens: [5](#0-4) 

Unlike `UnfreezeBalance`/`WithdrawExpireUnfreeze`, which separate "resource is frozen" state from "funds are claimable" state and allow reclaiming already-owed balances regardless of ongoing freeze/vote state, `ExchangeWithdrawActuator` conflates "pool is tradeable" state with "creator can reclaim reserves" state — exactly the anti-pattern described in the Derby report, where a business-logic "off" flag is incorrectly reused to gate a fund-recovery path.

A pool side can legitimately reach exactly zero through repeated/aggregated trading via `ExchangeTransactionActuator`, since `ExchangeCapsule.transaction()` allows the balance to be reduced arbitrarily close to (and, given integer truncation of the bancor-style formula, potentially to) zero without any lower-bound check beyond `hardenedCalc` mode's `>= 0` assertion: [6](#0-5) 
Since `ExchangeTransactionActuator` is reachable by any unprivileged account (it is not restricted to the creator), an attacker or even normal market activity can drive one side of the pool to zero, and thereafter the creator's remaining reserve on the other side becomes permanently unwithdrawable through the intended `ExchangeWithdraw` path.

### Impact Explanation
Once one side of an exchange pool balance reaches zero, the pool creator's remaining balance on the other side is durably locked in the `Exchange`/`ExchangeV2` capsule with no actuator-level path to recover it: `ExchangeWithdrawActuator` refuses to execute, `ExchangeInjectActuator` also refuses (so the creator cannot "top up" the drained side to re-enable trading and later withdraw), and there is no governance/proposal-level rescue path for individual exchange pools. This constitutes a permanent freezing-of-funds condition for the creator's TRX/TRC-10 assets locked in the pool, reachable purely through normal `ExchangeTransactionActuator` trading by any account (no privileged role required to trigger the zero-balance state).

### Likelihood Explanation
Reaching a zero balance requires draining one side of a specific exchange pool via a sequence of `ExchangeTransactionActuator` trades (bounded only by `dynamicStore.getExchangeBalanceLimit()` and available caller asset balance), which is plausible for low-liquidity pools or via targeted, well-funded trading, and does not require compromising any privileged component. Once triggered, the impact is deterministic and permanent for that pool.

### Recommendation
Decouple the "exchange is tradeable" invariant from the "creator can withdraw remaining reserves" invariant, mirroring how `UnfreezeBalanceV2`/`WithdrawExpireUnfreezeActuator` separate resource-lock state from fund-claim state. Specifically, remove (or relax) the `firstTokenBalance == 0 || secondTokenBalance == 0` guard in `ExchangeWithdrawActuator.doValidate()` (and, if injection is meant to allow recovery, in `ExchangeInjectActuator` as well), and instead only validate that the specific withdrawal amount does not exceed the current available balance on each side, allowing the creator to withdraw any nonzero remaining reserve even after the pool has become untradeable.

### Proof of Concept
1. Creator calls `ExchangeCreateActuator` to create an exchange pool with `firstTokenBalance = A`, `secondTokenBalance = B` (both > 0), becoming `exchangeCapsule.getCreatorAddress()`.
2. Any unprivileged account repeatedly calls `ExchangeTransactionActuator` selling the first token into the pool until `secondTokenBalance` (the bought-out side) is driven down to `0` (bounded trade sizes bring it arbitrarily close to zero; integer truncation in `ExchangeProcessor`/`ExchangeCapsule.transaction()` can produce an exact `0` result for the drained side) — validated only against `dynamicStore.getExchangeBalanceLimit()` and the trader's own asset balance, per `ExchangeTransactionActuator` validation at `actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java:194-197`.
3. Creator now calls `ExchangeWithdrawActuator` to withdraw the remaining nonzero `firstTokenBalance`.
4. `doValidate()` reads `secondTokenBalance == 0` and throws `ContractValidateException("Token balance in exchange is equal with 0, the exchange has been closed")` at `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java:209-212`, before any state is mutated.
5. The creator has no other actuator path to reclaim the remaining `firstTokenBalance`: `ExchangeInjectActuator` is blocked by the identical check, and `ExchangeWithdrawActuator` is the only withdrawal path. The remaining reserve is permanently frozen in the `Exchange`/`ExchangeV2` capsule.

**Uncertainty note:** I was not able to fully trace `ExchangeProcessor`'s exact bancor-formula arithmetic (file `chainbase/src/main/java/org/tron/core/capsule/utils/ExchangeProcessor` was not retrieved in full) to mathematically confirm that a non-hardened trade can drive a balance to *exactly* zero versus only asymptotically close to it; this would need to be verified directly in the repository before treating step 2 as fully deterministic. However, `ExchangeCapsule.transaction()` with `hardenedCalc=true` explicitly permits and validates results down to `>= 0`, so the zero-balance state is at minimum reachable in that code path.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L181-183)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L185-212)
```java
    byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
    byte[] secondTokenID = exchangeCapsule.getSecondTokenId();
    long firstTokenBalance = exchangeCapsule.getFirstTokenBalance();
    long secondTokenBalance = exchangeCapsule.getSecondTokenBalance();

    byte[] tokenID = contract.getTokenId().toByteArray();
    long tokenQuant = contract.getQuant();

    long anotherTokenQuant;

    if (dynamicStore.getAllowSameTokenName() == 1
        && !Arrays.equals(tokenID, TRX_SYMBOL_BYTES)
        && !isNumber(tokenID)) {
      throw new ContractValidateException("token id is not a valid number");
    }

    if (!Arrays.equals(tokenID, firstTokenID) && !Arrays.equals(tokenID, secondTokenID)) {
      throw new ContractValidateException("token is not in exchange");
    }

    if (tokenQuant <= 0) {
      throw new ContractValidateException("withdraw token quant must greater than zero");
    }

    if (firstTokenBalance == 0 || secondTokenBalance == 0) {
      throw new ContractValidateException("Token balance in exchange is equal with 0,"
          + "the exchange has been closed");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L194-197)
```java
    if (firstTokenBalance == 0 || secondTokenBalance == 0) {
      throw new ContractValidateException("Token balance in exchange is equal with 0,"
          + "the exchange has been closed");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L200-203)
```java
    if (firstTokenBalance == 0 || secondTokenBalance == 0) {
      throw new ContractValidateException("Token balance in exchange is equal with 0,"
          + "the exchange has been closed");
    }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L160-167)
```java
    if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
      throw new ContractValidateException("Exchange balance must be >=0 after transaction");
    }
    this.exchange = this.exchange.toBuilder()
        .setFirstTokenBalance(newFirstTokenBalance)
        .setSecondTokenBalance(newSecondTokenBalance)
        .build();

```
