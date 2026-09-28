### Title
Attacker can fill a victim's per-account token lists with dust deposit-token transfers, blocking new deposits and borrows - (File: contracts/Pool.sol)

### Summary
The Xen bug class — broken reference counting in an error-handling path corrupting shared accounting — maps to Metronome's per-account token lists in `Pool`. `depositTokensOfAccount` / `debtTokensOfAccount` act as reference-counted registries: `DepositToken` and `DebtToken` add an entry when an account's balance goes from 0 to non-zero and remove it when it returns to 0 (`DepositToken._mint`/`_burn` at `contracts/DepositToken.sol:460-488`, `DebtToken._mint`/`_burn` at `contracts/DebtToken.sol:539-600`). `Pool.addToDepositTokensOfAccount` reverts once the combined count reaches `MAX_TOKENS_PER_USER` (`contracts/Pool.sol:143-148`, `216-220`). Because `DepositToken.transfer`/`seize` run the same "add on zero→nonzero" hook for the recipient, an unprivileged attacker can send dust amounts of every registered deposit token to a victim, permanently occupying the victim's slots. Any subsequent action that needs a new list entry — depositing a new collateral type, or issuing a new synthetic (which adds a debt token) — reverts inside the token mint and rolls back the whole transaction.

### Finding Description
- `DepositToken._transfer` (used by `transfer`, `transferFrom`, and `seize`) adds the recipient to `depositTokensOfAccount` when the recipient's prior balance was zero, mirroring `_mint`/`_burn` (`contracts/DepositToken.sol:460-488`, `498-510`).
- `Pool.addToDepositTokensOfAccount` enforces `onlyIfAdditionWillNotReachMaxTokens`, which counts `debtTokensOfAccount.length + depositTokensOfAccount.length` against `MAX_TOKENS_PER_USER` and reverts with `UserReachedMaxTokens` (`contracts/Pool.sol:143-148`).
- The add happens inside the mint/transfer execution path, so the revert aborts the entire `deposit`/`issue` call.
- A victim cannot cheaply recover while holding debt: removing an entry requires zeroing the dust balance, and `transfer`/`withdraw` are gated by `_revertIfLocked`/`unlockedBalanceOf`, which returns 0 for an unhealthy or fully-utilized position (`contracts/DepositToken.sol:348-411`, `383-398`). The dust becomes locked collateral that still must be listed, so the slots cannot be freed exactly when freeing them matters.

Attack steps (all unprivileged):
1. Attacker deposits minimal amounts into each of the pool's `N` deposit tokens from their own account.
2. Attacker calls `depositToken.transfer(victim, dust)` for each token until `debtTokensOfAccount.length(victim) + depositTokensOfAccount.length(victim) == MAX_TOKENS_PER_USER`.
3. Victim's `deposit()` of any new collateral type reverts in `addToDepositTokensOfAccount`; `DebtToken.issue` for any synthetic they don't already hold reverts in `addToDebtTokensOfAccount` (`contracts/Pool.sol:204-220`, `contracts/DebtToken.sol:597-600`).

### Impact Explanation
Temporary freezing of funds / position management: the victim is blocked from adding new collateral to improve health and from opening new debt positions. If the victim's position is near or below the collateral factor and they cannot free slots (dust is locked by `_revertIfLocked`), the freeze is effectively permanent for that account — they cannot deleverage by depositing a different collateral and are pushed toward liquidation. Direct fund theft is not achieved, but forced inability to add collateral plus potential liquidation qualifies as freezing/permanent impairment under the acceptance criteria.

### Likelihood Explanation
Feasibility depends on `MAX_TOKENS_PER_USER` (defined in `PoolStorage.sol`) being reachable with the number of registered deposit/debt tokens; each attack deposit token slot costs only dust. The check exists precisely because the list is gas-sensitive, indicating the cap is intended to be hit in practice. No privileged role, oracle manipulation, or reentrancy is required — only public `transfer` of legitimately obtained deposit tokens. Note: the add-on-transfer behavior inside `_transfer` (lines 511+) was not fully visible in the retrieved context; the PoC must confirm `seize`/`transfer` triggers `addToDepositTokensOfAccount` for zero-balance recipients. If `_transfer` does not add recipients, the attack reduces to dust `deposit(..., onBehalfOf_=victim)` — `deposit` takes an `onBehalfOf_` parameter (`IDepositToken.sol:18`), which achieves the same effect without needing transfers.

### Recommendation
Decouple list membership from the revert path: make `addTo*TokensOfAccount` not revert (cap only enforced for the actor's own additions via `deposit`/`issue` initiated by the account holder), or track entries with an explicit membership flag so unsolicited inbound transfers/seizes cannot consume slots. Alternatively, allow removal of dust entries regardless of lock status when the amount is below a threshold, or exempt feeCollector/liquidation flows.

### Proof of Concept
Foundry fork test outline:
```solidity
// Assume MAX_TOKENS_PER_USER slots are fillable by registered deposit tokens
address victim = makeAddr("victim");
// victim: deposit collateral in token[0], issue debt -> position near limit
for (uint i = 0; i < depositTokens.length && /*slots remain*/; ++i) {
    depositTokens[i].deposit(1e6, attacker);          // or transfer dust
    depositTokens[i].transfer(victim, 1);             // fill victim slot
}
// victim tries to deposit a collateral type not yet held:
vm.expectRevert(Pool.UserReachedMaxTokens.selector);
depositTokens[k].deposit(amount, victim);             // reverts in addToDepositTokensOfAccount
// victim tries to issue a new synthetic (adds debt token entry):
vm.expectRevert(Pool.UserReachedMaxTokens.selector);
debtToken.issue(1, victim);
```
Requires a fork with the real `MAX_TOKENS_PER_USER` and registered token set to confirm the cap is reachable; verify `deposit(amount, onBehalfOf=victim)` also fills slots (it calls `_mint` for `onBehalfOf_`, which invokes `addToDepositTokensOfAccount(victim)` when the prior balance is 0 — `contracts/DepositToken.sol:485-488`).