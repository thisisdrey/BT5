### Title
Decreasing `ExchangeBalanceLimit` via committee proposal permanently freezes trading and injection on existing exchange pairs - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java])

### Summary
`ExchangeBalanceLimit` is a dynamic-property parameter (adjustable through the standard proposal mechanism) that caps the token balance an exchange pool may hold. `ExchangeInjectActuator`, `ExchangeCreateActuator`, and `ExchangeTransactionActuator` all enforce `newTokenBalance > balanceLimit || newAnotherTokenBalance > balanceLimit` before allowing the operation, but `ExchangeWithdrawActuator` does not perform this check. If the committee lowers `ExchangeBalanceLimit` below the current balance already held by an existing exchange pair, every future trade or injection on that pair will be rejected, because a trade always increases at least one side of the pool's balance, which will now always exceed the new (lower) limit.

### Finding Description
In `ExchangeTransactionActuator.doValidate()`: [1](#0-0) 
the same pattern (`long balanceLimit = dynamicStore.getExchangeBalanceLimit(); if (newTokenBalance > balanceLimit || newAnotherTokenBalance > balanceLimit) { throw ... }`) is applied in `ExchangeCreateActuator`, `ExchangeInjectActuator`, and `ExchangeTransactionActuator` (confirmed via grep matches in all three files). This check compares the **resulting** balance of the pool after the operation against the current value of `dynamicStore.getExchangeBalanceLimit()`, not against the balance at the time the pool was created.

Since a buy/sell trade on `ExchangeTransactionActuator` necessarily *increases* one side of the pool and decreases the other, any pre-existing pool whose balance is already above a newly-lowered `ExchangeBalanceLimit` will fail this check on **every subsequent trade**, no matter how small, because the increased side's `newTokenBalance` will always exceed the reduced `balanceLimit`. The same applies to `ExchangeInjectActuator`, blocking further liquidity injections.

`ExchangeWithdrawActuator.doValidate()` (see the full validate/execute flow) performs no such `ExchangeBalanceLimit` check — it only validates precision, non-zero balances, and creator ownership — so withdrawals by the exchange creator remain possible, but ordinary trading by any user against that pair becomes permanently impossible once the limit is lowered below the pool's balances, exactly mirroring the "decrease-in-limit blocks legitimate, already-compliant state" bug class from the reference report (a wallet balance limit that blocks transfers once decreased below a holder's balance, whereas the recommendation there was to only enforce the check on the increasing operation, not universally).

### Impact Explanation
Once `ExchangeBalanceLimit` is decreased below an existing pool's balance (a routine, non-malicious governance/parameter-tuning action, comparable to the original report's `setPerWalletLimit` decrease), any unprivileged user attempting `ExchangeTransactionContract` (trade) or `ExchangeInjectContract` (add liquidity) against that pair will have their transaction rejected with `"token balance must less than " + balanceLimit"`, permanently freezing that trading pair for all market participants — this is not a temporary state, it recovers only if the committee raises the limit again or the exchange creator entirely drains the pool via `ExchangeWithdrawActuator` to below the new limit. This constitutes a permanent freezing-of-funds/functionality condition for an on-chain exchange pair reachable by any order-placing user.

### Likelihood Explanation
Likelihood is Medium: it requires the committee to actually lower `ExchangeBalanceLimit` (a legitimate, foreseeable maintenance action, not an attack), after which the freeze triggers automatically and deterministically for any pool whose balance already exceeds the new limit — no attacker action is needed beyond a normal trade attempt. The bug pattern is structurally identical to the referenced Tokemak issue: the code comment/intent ("existing state shouldn't be broken by future limit decreases") is violated because the check is unconditionally applied to the resulting balance of forward operations (trade/inject) rather than only to originally-limit-compliant balances.

### Recommendation
Skip or bypass the `ExchangeBalanceLimit` check in `ExchangeTransactionActuator` and `ExchangeInjectActuator` when the pool's balance is already above the limit and the operation does not increase the "excess" (e.g., only enforce the cap when the pre-operation balance was already within the limit), or grandfather already-created pools/balances that exceed a subsequently lowered limit, similar to the "check limit only on mint/increase from a compliant baseline" recommendation in the source report.

### Proof of Concept
1. Committee creates a proposal to increase `ExchangeBalanceLimit` from default and it passes; a user creates an exchange pair via `ExchangeCreateActuator` and injects liquidity such that `firstTokenBalance`/`secondTokenBalance` reach, e.g., 2,000,000 units — within the then-current `ExchangeBalanceLimit`.
2. Committee later proposes and passes a decrease of `ExchangeBalanceLimit` to 1,000,000 (a normal parameter adjustment, not an attack).
3. Any user broadcasts an `ExchangeTransactionContract` to trade even 1 unit of the first token for the second token. In `ExchangeTransactionActuator`/`ExchangeInjectActuator` validate logic (same pattern shown in `ExchangeInjectActuator.java:233-236`), `newTokenBalance` (≈2,000,000) `> balanceLimit` (1,000,000) is now always true, so the transaction fails with `ContractValidateException("token balance must less than 1000000")`.
4. This failure occurs for every subsequent trade or injection attempt on this pair, by any user, indefinitely — only `ExchangeWithdrawActuator` (creator-only, no limit check) can reduce the balance, or the committee must raise the limit again.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L233-236)
```java
    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    if (newTokenBalance > balanceLimit || newAnotherTokenBalance > balanceLimit) {
      throw new ContractValidateException("token balance must less than " + balanceLimit);
    }
```
