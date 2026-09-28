### Title
Griefing: forced dust deposits/transfers fill a victim's `MAX_TOKENS_PER_USER` slot list and DoS new deposits, mints, and token receipts — (File: contracts/DepositToken.sol)

### Summary
Any unprivileged account can call `DepositToken.deposit(amount_, onBehalfOf_)` (or the public `transfer`) with `onBehalfOf_`/recipient set to a victim. Minting or transferring even 1 wei of `msdTOKEN` to an address with a zero balance calls `pool.addToDepositTokensOfAccount(victim)`, permanently occupying one of the victim's `MAX_TOKENS_PER_USER = 30` slots (`Pool.sol:79`, `Pool.sol:143-148`, `DepositToken.sol:486-488`, `DepositToken.sol:518-520`). By dusting every whitelisted deposit token to a victim — and, if the victim is max-leveraged, additionally front-running so the victim cannot clear entries — the attacker makes every subsequent call that would add a new token entry to the victim's account revert with `UserReachedMaxTokens`.

### Finding Description
- `DepositToken.deposit` accepts an arbitrary `onBehalfOf_` beneficiary and mints to it without consent (`DepositToken.sol:211-237`). `_mint` adds the token to the recipient's per-account set when the prior balance was 0 (`DepositToken.sol:481-488`).
- `DepositToken.transfer`/`transferFrom` do the same on the recipient side (`DepositToken.sol:498-520`).
- `Pool.onlyIfAdditionWillNotReachMaxTokens` reverts once `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` (`Pool.sol:143-148`), and the check runs *before* the add in `addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` (`Pool.sol:204-220`).
- Once the victim's list is full:
  - Any `deposit` of a collateral the victim doesn't already hold reverts (mint → add → revert).
  - `DebtToken.issue` for any new synthetic reverts at `_mint` → `addToDebtTokensOfAccount`.
  - Incoming `transfer`/`seize` of any new msdTOKEN reverts.
- The victim can only free a slot by sending the dust away via `transfer`/`withdraw`, which requires `unlockedBalanceOf(victim) >= dust`. If the victim has debt and is at/near max leverage, `unlockedBalanceOf` can return 0 (`DepositToken.sol:383-398`), so the dust is locked and the victim cannot self-clean until they repay debt — repaying debt itself is still possible (repay only removes entries), so the freeze is temporary but bounded by the victim's ability to deleverage.

### Impact Explanation
Temporary freezing of funds/protocol liveness DoS on a per-victim basis: the victim cannot open new collateral positions, mint new synthetics, or receive msdTOKEN transfers until slots are freed; if fully leveraged, they cannot free slots without first repaying debt, which may itself be economically painful (e.g., they are blocked from adding collateral needed to restore health). Cost to the attacker is ~30 dust deposits/transfers worth of underlying dust + gas — analogous to the reference bug's "cheap repetitive packets take down the node" profile.

### Likelihood Explanation
Requires the pool to list enough distinct deposit+debt tokens that the victim's list can actually reach 30 (attack feasibility depends on the deployed token count; on deployments with few collaterals the list cannot be filled). Requires no privileged role, no oracle manipulation, and only public entry points (`deposit`, `transfer`). The check is order-sensitive: the attacker can front-run any of the victim's cleanup attempts with fresh dust on a different token while supply lasts.

### Recommendation
- Remove the per-account token cap, or make `addTo*` idempotent without a hard cap (track positions differently, e.g., iterate whitelisted tokens).
- Alternatively, gate `deposit(..., onBehalfOf_)`/`transfer` so a token is only added to an account's list via an opt-in path, or provide a `leaveDepositToken` escape that burns/returns dust regardless of locked balance (dust has negligible collateral value).
- At minimum, exclude tokens whose balance is below a dust threshold from counting toward `MAX_TOKENS_PER_USER`.

### Proof of Concept
Foundry/Hardhat fork skeleton:

```solidity
// Assume pool has >= N whitelisted DepositTokens such that victim's
// depositTokens+debtTokens count can reach MAX_TOKENS_PER_USER (30).
address victim = ...;
uint256 slots = 30 - pool.getDepositTokensOfAccount(victim).length
                 - pool.getDebtTokensOfAccount(victim).length;

for (uint i; i < slots; ++i) {
    IDepositToken dt = depositTokens[i];           // whitelisted collaterals
    IERC20 underlying = dt.underlying();
    deal(address(underlying), attacker, 2);        // dust
    underlying.approve(address(dt), 2);
    dt.deposit(1, victim);                         // mints dust to victim,
    // -> DepositToken._mint -> pool.addToDepositTokensOfAccount(victim)
    assertTrue(listContains(victim, dt));
}
// victim's list is now at 30.

// Victim tries to deposit a collateral they don't hold yet:
vm.prank(victim);
vm.expectRevert(Pool.UserReachedMaxTokens.selector);
newDepositToken.deposit(1e18, victim);

// Victim tries to mint a new synthetic:
vm.prank(victim);
vm.expectRevert(Pool.UserReachedMaxTokens.selector);
newDebtToken.issue(1e18, victim);

// If victim.isFullyLeveraged: unlockedBalanceOf(victim) == 0 on dust tokens,
// so transfer(dust) also reverts -> cleanup requires repaying debt first.
```

Caveats I could not fully verify within the available exploration: the exact number of whitelisted deposit/debt tokens on each live deployment (the attack only works if a victim's list can actually be filled to 30 — `deployments/` suggests multiple chains with differing token sets), and whether this slot-filling griefing was already flagged in a prior Metronome audit (it is a documented design trade-off in similar codebases, which the scope rules may treat as a known issue).