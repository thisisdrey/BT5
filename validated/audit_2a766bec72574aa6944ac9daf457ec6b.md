### Title
Dust deposit-token transfers fill a victim's `MAX_TOKENS_PER_USER` slots, blocking new collateral deposits and debt positions - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool` enforces a hard cap of `MAX_TOKENS_PER_USER = 30` on the combined `depositTokensOfAccount + debtTokensOfAccount` lists, reverting with `UserReachedMaxTokens` whenever a new token would be added. Any ERC-20 `transfer`/`transferFrom`/`seize`/`deposit(onBehalfOf)` on a `DepositToken` adds the recipient to `depositTokensOfAccount` when the recipient's prior balance is zero — with no minimum-amount check and no recipient opt-in. An unprivileged attacker can therefore dust-transfer every whitelisted deposit token to a victim, permanently occupying their slots until the victim fully empties each token.

### Finding Description
In `DepositToken._transfer` (`contracts/DepositToken.sol:498-526`), whenever `_recipientBalanceBefore == 0 && amount_ > 0`, the pool's `addToDepositTokensOfAccount(recipient_)` is called. `Pool.addToDepositTokensOfAccount` (`contracts/Pool.sol:216-220`) applies `onlyIfAdditionWillNotReachMaxTokens` (`contracts/Pool.sol:143-148`), so once an account holds 30 distinct tokens, *any* action that would add a new token reverts.

Reachable attacker path, all public entry points:

1. Attacker deposits 1 wei of each whitelisted underlying into every `DepositToken` (`DepositToken.deposit`), then calls `transfer(victim, 1)` on each — filling the victim's deposit-token slots up to the number of whitelisted deposit tokens (the pool itself can hold up to 30, `ReachedMaxDepositTokens`).
2. Attacker additionally calls `DebtToken.issue(dust, attacker)` — wait, debt is minted to the caller — but the debt-token slot attack is instead reachable via `SmartFarmingManager` leverage flows or simply because the victim's own debt tokens count toward the same 30-slot budget.
3. After the victim's list reaches 30 combined entries:
   - `DepositToken.deposit(amount, victim)` for any collateral type the victim does not already hold reverts in `_mint` → `addToDepositTokensOfAccount` → `UserReachedMaxTokens`.
   - Any `DebtToken` mint to the victim (e.g., via leverage) reverts the same way.
   - New deposit-token `transfer`s to the victim revert.

Removal requires the recipient's balance to hit zero (`removeFromDepositTokensOfAccount` at `Pool.sol:630` / `DepositToken._transfer` line 523, `_burn` line 460). If the victim has outstanding debt, `withdraw`/`transfer` on the dusted tokens is blocked by `_revertIfLocked`/`unlockedBalanceOf` — a 1-wei dust balance of a collateral the victim never wanted is still counted in `depositOf` and can remain locked, so the victim cannot free a slot without first repaying debt. No modifier (`whenNotShutdown`, `nonReentrant`, `SynthContext`, pause flags) prevents the dust transfer itself.

### Impact Explanation
- **Liveness / temporary freezing of funds**: A victim with open debt cannot onboard a new collateral type (e.g., to top up an unhealthy position) because every `deposit`/`transfer` of a novel `DepositToken` reverts with `UserReachedMaxTokens`. Since dust positions may be locked by existing debt, the victim cannot clear slots without first repaying — yet adding collateral is precisely what they need to avoid liquidation.
- **Liquidation coercion**: An attacker can dust-fill a borderline account so the victim cannot deposit fresh collateral to restore health, then liquidate them via `Pool.liquidate`, harvesting the liquidation incentive that would otherwise have been prevented.

This is the same bug class as the advisory: an unguarded input path (zero-cost dust insertion into a fixed-capacity list) turns a routine call into a denial of service for the affected account.

### Likelihood Explanation
- Requires only an EOA, dust amounts of whitelisted underlyings, and public `transfer`/`deposit` calls — fully within the attacker model.
- Feasibility depends on the deployed pool having enough whitelisted deposit + debt tokens to approach 30; the pool itself caps deposit tokens at 30 (`addDepositToken`, `Pool.sol:703`), so the ceiling is reachable by design. Even partial filling reduces the victim's usable collateral diversity.
- Cost is bounded (30 dust deposits + 30 transfers); no privileged role, oracle manipulation, or governance action needed.

### Recommendation
- Add a minimum first-deposit/first-transfer threshold in `DepositToken._mint`/`_transfer` (e.g., only call `addToDepositTokensOfAccount` when `amount_ >= MIN_DUST` or when the resulting balance exceeds a USD floor via `masterOracle().quoteTokenToUsd`), and/or
- Allow `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` to silently skip or auto-remove dust entries instead of reverting the parent transfer, and/or
- Provide a `pool.removeDustToken(account, token)` escape that lets an account force-remove a deposit token whose balance is below a dust threshold regardless of lock status.

### Proof of Concept
Foundry/Hardhat sketch against a live-fork deployment:

```solidity
// Assume pool has N whitelisted DepositTokens d[0..N-1] and victim has debt
// such that victim.slots() + N >= 30.

for (uint i; i < N; ++i) {
    IDepositToken dt = depositTokens[i];
    IERC20 underlying = dt.underlying();
    deal(address(underlying), attacker, 1);
    underlying.approve(address(dt), 1);
    dt.deposit(1, attacker);          // attacker mints 1 wei of msdTOKEN
    dt.transfer(victim, 1);           // adds dt to victim's depositTokensOfAccount
}

// victim now at MAX_TOKENS_PER_USER
assertEq(pool.getDepositTokensOfAccount(victim).length + pool.getDebtTokensOfAccount(victim).length, 30);

// Any deposit of a collateral type victim doesn't hold reverts
vm.prank(victim);
vm.expectRevert(Pool.UserReachedMaxTokens.selector);
newDepositToken.deposit(1e18, victim);

// Victim cannot clear a dust slot while debt locks it
vm.prank(victim);
vm.expectRevert(DepositToken.NotEnoughFreeBalance.selector);
depositTokens[0].transfer(attacker, 1); // or withdraw — dust is locked
```

A Hardhat variant mirrors `test/Pool.test.ts:1386-1416`, but replacing the fake-token loop with real `DepositToken.deposit` + `transfer` dust calls from an attacker account, then asserting `UserReachedMaxTokens` on the victim's subsequent `deposit`.