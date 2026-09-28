### Title
Dust deposits/transfers fill a victim's `depositTokensOfAccount` set up to `MAX_TOKENS_PER_USER`, permanently blocking the victim from adding new collateral types (forced liquidation / position freeze) - ([File: contracts/DepositToken.sol](contracts/DepositToken.sol))

### Summary
The zeroconf bug class — an unauthenticated caller forcing unbounded (here: cap-bounded but attacker-fillable) per-address state accumulation that exhausts a limit — maps onto `Pool.depositTokensOfAccount` / `debtTokensOfAccount`. Anyone can add a deposit token to an arbitrary victim's per-account set by calling `DepositToken.deposit(1 wei, victim)` or `transfer(victim, 1 wei)`, because both `_mint` and `_transfer` unconditionally call `pool.addToDepositTokensOfAccount(recipient_)` when the recipient's prior balance is zero. Once the combined `debt + deposit` set reaches `MAX_TOKENS_PER_USER = 30`, `onlyIfAdditionWillNotReachMaxTokens` reverts every subsequent addition, which bricks deposits of, transfers of, and (via `seize`) liquidations paying out any token the victim/liquidator doesn't already hold.

### Finding Description
- `DepositToken._mint` (DepositToken.sol:486-488) and `DepositToken._transfer` (DepositToken.sol:518-525) call `pool.addToDepositTokensOfAccount(account_)`/`(recipient_)` whenever the balance goes `0 → >0`. There is no opt-in; the recipient cannot refuse.
- `Pool.addToDepositTokensOfAccount` (Pool.sol:216-220) is guarded by `onlyIfAdditionWillNotReachMaxTokens` (Pool.sol:143-148), which reverts with `UserReachedMaxTokens` once `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30`.
- `deposit(amount_, onBehalfOf_)` (DepositToken.sol:211-237) has no minimum amount and no consent check on `onBehalfOf_`; the only cost to the attacker is 1 wei of underlying (plus deposit fee rounding to ~0) per token, per victim.
- Revert propagation: the revert happens inside `_mint`/`_transfer`, so the *entire* user operation reverts — `deposit`, `transfer`, `transferFrom`, `NativeTokenGateway.deposit`, `SmartFarmingManager` flows, and `Pool.liquidate`→`seize` (Pool.sol:343-345 → `_transfer`) all fail when the receiving account's set is full and the token is new to it.

Attack:
1. Victim has an open debt position collateralized by token A only (1 slot used).
2. Attacker calls `deposit(1, victim)` (or `transfer(victim, 1)`) on the other 29 deposit tokens in the pool. Victim's set is now at the cap; entries persist as long as each dust balance is > 0 — the victim cannot remove them without transferring each dust balance away (each outbound transfer also works only if the *recipient's* set accepts the token), and each removal costs gas.
3. If collateral A's price drops, the victim tries to deposit collateral B to restore health — `addToDepositTokensOfAccount` reverts, the deposit fails. The victim cannot top up with any collateral type they don't already hold.
4. Any liquidator whose own account set is full and who doesn't already hold token A cannot liquidate the victim either, because `seize`→`_transfer`→`addToDepositTokensOfAccount(liquidator)` reverts — narrowing the liquidator set and delaying liquidation further.

### Impact Explanation
Temporary freezing of funds / forced liquidation. The victim is prevented from depositing any new collateral type, so an unhealthy position cannot be rescued except by repaying debt or acquiring more of an already-held token; the position is pushed into liquidation where it otherwise would have been topped up (loss via liquidation fee/discount). Additionally, any transfer of a not-yet-held msdTOKEN to the victim reverts, freezing receipt of collateral tokens. The attacker pays only dust per slot.

### Likelihood Explanation
Requires a pool with ≥30 combined deposit/debt token offerings (or fewer if the victim already occupies slots — realistic pools with many msAssets/msdTokens approach this), and a victim with a live position. No privileged role needed; `deposit(onBehalfOf_)` is permissionless. Cost is linear in slots at 1 wei + gas each. Mitigating factor: cap is per-pool and bounded at 30, and the victim can recover by spending gas to push out dust balances — hence griefing / temporary freeze rather than permanent loss, consistent with the Medium severity of the source advisory.

### Recommendation
- Only add to `depositTokensOfAccount` on explicit, beneficiary-initiated actions, or make `addToDepositTokensOfAccount` tolerant (skip/no-op at cap instead of revert) and have `depositOf`/`debtPositionOf` iterate over held balances rather than the set.
- Alternatively enforce a minimum first-deposit amount, or let any account permissionlessly remove a token entry from its own set via a `sweepTokenFromAccountList` helper so a single transaction can clear all dust entries.

### Proof of Concept
```solidity
// Hardhat/Foundry fork sketch against deployed Pool + DepositTokens
// given: pool has >= 30 registered deposit tokens (dt[0..29]), victim holds dt[0] only
for (uint i = 1; i < 30; i++) {
    underlying[i].approve(address(dt[i]), 1);
    dt[i].deposit(1, victim);          // _mint -> addToDepositTokensOfAccount(victim)
}
assertEq(pool.getDepositTokensOfAccount(victim).length, 30);

// victim attempts to deposit a collateral type not yet held -> reverts
vm.prank(victim);
underlyingNew.approve(address(dtNew), 1e18);
vm.expectRevert(Pool.UserReachedMaxTokens.selector);
dtNew.deposit(1e18, victim);

// same for a plain transfer of an unheld msdTOKEN
vm.expectRevert(Pool.UserReachedMaxTokens.selector);
dtNew.connect(attacker).transfer(victim, 1);

// liquidator with a full set also cannot seize an unheld token:
vm.expectRevert(Pool.UserReachedMaxTokens.selector);
pool.connect(liquidator).liquidate(synth, amountToRepay, victim, dtA);
```