### Title
Dust deposit-token transfers fill a victim's per-account token list to `MAX_TOKENS_PER_USER`, blocking collateral deposits and debt minting needed to rescue an unhealthy position - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool` tracks per-account `depositTokensOfAccount`/`debtTokensOfAccount` lists capped at `MAX_TOKENS_PER_USER = 30` (`contracts/Pool.sol:79`). `DepositToken._transfer` unconditionally calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's balance goes from 0 to >0 (`contracts/DepositToken.sol:518-519`), and `addToDepositTokensOfAccount` reverts with `UserReachedMaxTokens` once the combined count reaches 30 (`contracts/Pool.sol:143-148, 216-220`). An unprivileged attacker can deposit dust into every deposit token and `transfer` 1 wei of each to a victim, saturating the victim's list. Every subsequent `deposit` of a new collateral type, `mint`/`issue` of a new synthetic debt, and any incoming deposit-token transfer to the victim reverts, so the victim cannot add a new collateral to restore health and gets liquidated.

### Finding Description
- Entry: `DepositToken.transfer(to_, 1)` — public, no minimum amount; `_revertIfLocked` only constrains the *sender* (`contracts/DepositToken.sol:348-354`).
- `_transfer` → `pool.addToDepositTokensOfAccount(to_)` when `_recipientBalanceBefore == 0 && amount_ > 0` (`contracts/DepositToken.sol:518-519`).
- `Pool.addToDepositTokensOfAccount` enforces `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30 → UserReachedMaxTokens` (`contracts/Pool.sol:143-148`).
- Consequently the victim's `deposit()` into any *new* `DepositToken` reverts inside `_mint` → `addToDepositTokensOfAccount` (`contracts/DepositToken.sol:486-488`), and `DebtToken.issue/mint` of a new synthetic reverts via `addToDebtTokensOfAccount` (`contracts/Pool.sol:204-208`).

Invariant broken: liveness — a user cannot top-up collateral or open a new debt position, so a position drifting toward the liquidation threshold cannot be saved by depositing a different collateral type (the only collateral already held can still be topped up, but only if the victim already holds that token).

### Impact Explanation
An attacker can cheaply fill a target's token list (cost: dust deposits across ~15–30 collaterals, some recoverable via withdraw). If the victim's position later becomes liquidatable and the cure requires depositing a collateral the victim doesn't already hold (or issuing a debt token for a `swap` to repay), the rescue tx reverts — the position is force-liquidated, costing the victim the liquidation incentive/seizure penalty. For contract-based accounts (multisigs, smart wallets without arbitrary ERC20 transfer calls to the dust tokens), the freeze on new deposits/debts is effectively permanent, since the only way to shrink the list is transferring each dust balance back out. This maps to the DoS/crash bug class (CVE-2018-3173, availability-only impact): it denies the victim the ability to use core protocol functions and indirectly enables loss of funds via forced liquidation.

### Likelihood Explanation
Medium. Requires an attacker to front-run/observe a vulnerable victim and spend gas on up to ~30 dust transfers; exploitation is only profitable when the victim has a live debt position near liquidation that cannot be topped up with a collateral type already in their list. It is fully permissionless — no privileged role, oracle manipulation, or governance action needed — and there is no on-chain mitigation: `MAX_TOKENS_PER_USER` is a hard constant, the add happens before any health check, and no sweep function exists for the account list.

### Recommendation
- Only add the token to `depositTokensOfAccount` above a minimum meaningful amount (e.g., a non-zero threshold set per deposit token, or on `deposit()` rather than on every `transfer`), or
- Track membership lazily: compute deposit/debt token membership from balances at read time, or
- Let a user (or anyone) prune entries whose balance is below a dust threshold via a `removeFromDepositTokensOfAccount` escape callable by the account itself.

### Proof of Concept
Foundry fork test sketch:

```solidity
// victim holds msdA (deposit token A) and has msUSD debt
// attacker:
for (uint i; i < 30 - victimTokenCount; ++i) {
    collateral[i].approve(address(depositToken[i]), type(uint256).max);
    depositToken[i].deposit(2);              // attacker gets msd_i
    depositToken[i].transfer(victim, 1);     // +1 entry in victim's depositTokensOfAccount
}
// victim now has 30 entries
vm.prank(victim);
vm.expectRevert(Pool.UserReachedMaxTokens.selector);
depositTokenB.deposit(amountB);              // new collateral -> reverts

vm.prank(victim);
vm.expectRevert(Pool.UserReachedMaxTokens.selector);
debtTokenEth.issue(1 ether);                 // new debt -> reverts

// price moves; victim cannot add collateral B
pool.liquidate(msUSD, victim, repayAmt, depositTokenA); // succeeds -> forced loss
```

References: `contracts/Pool.sol:79` (cap), `contracts/Pool.sol:143-148` (modifier), `contracts/Pool.sol:204-220` (list adds), `contracts/DepositToken.sol:348-354` (transfer entry), `contracts/DepositToken.sol:486-488, 518-519` (mint/transfer list insertion).