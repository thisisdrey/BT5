### Title
Attacker can fill a victim's `depositTokensOfAccount` list to `MAX_TOKENS_PER_USER` via dust `DepositToken` transfers, DoSing new collateral deposits and position top-ups - ([File: contracts/DepositToken.sol])

### Summary
The external report (CVE-2025-55521) is an authenticated DoS via a crafted request. The Metronome analog is an unprivileged account-list stuffing DoS: `Pool` caps the combined number of deposit and debt tokens tracked per account at `MAX_TOKENS_PER_USER = 30` via `onlyIfAdditionWillNotReachMaxTokens` (`contracts/Pool.sol:143-148`), and `DepositToken._transfer`/`_mint` push into that list whenever a recipient's balance goes from 0 to >0 (`contracts/DepositToken.sol:517-520`, `485-488`). Any EOA can deposit dust into each pool collateral and `transfer` 1 wei of each `DepositToken` to a victim, permanently occupying all 30 slots and making every subsequent `deposit`, `transfer`, or liquidation `seize` involving a *new* deposit token revert with `UserReachedMaxTokens` for that account.

### Finding Description
- `Pool.addToDepositTokensOfAccount` is permissioned only by `doesDepositTokenExist(msg.sender)`, and the `onlyIfAdditionWillNotReachMaxTokens` modifier reverts once `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` (`contracts/Pool.sol:143-148`, `204-220`).
- `DepositToken._transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` on first receipt with no opt-in by the recipient (`contracts/DepositToken.sol:517-520`). `transfer` only checks the *sender's* unlocked balance (`_revertIfLocked`), so an attacker needs no victim approval (`contracts/DepositToken.sol:348-354`).
- `DepositToken.deposit(amount, onBehalfOf)` mints via `_mint`, which hits the same path (`contracts/DepositToken.sol:211-237`, `485-488`), so even a third-party deposit on behalf of the victim, or the victim depositing a collateral type they don't yet hold, reverts once the list is full.
- `Pool.liquidate` seizes collateral via `depositToken_.seize(account, _msgSender, _toLiquidator)` which is `_transfer` under the hood (`contracts/DepositToken.sol:343-345`; `contracts/Pool.sol:587-593`). A liquidator whose own list is full cannot receive a new collateral type, and a victim whose list is full cannot be pushed *new* tokens — but critically the victim cannot add a new collateral type to restore health.

### Impact Explanation
The victim's `depositTokensOfAccount` list is permanently saturated with attacker-chosen dust positions. Until the victim manually empties and fully withdraws/transfers out each dust token (one slot freed per token only when their balance returns to 0, via `removeFromDepositTokensOfAccount`), they cannot deposit any collateral type they do not already hold. For an underwater or near-liquidation position this blocks the standard self-rescue path (deposit new collateral to raise `issuableLimitInUsd`), so `debtPositionOf` stays unhealthy and the position is force-liquidated — a liveness break converting into direct loss of funds. It also griefs the feeCollector and any integration contract holding many token types. This is a temporary/griefing DoS funded only by dust amounts of each underlying and gas; no privileged role, oracle manipulation, or governance action is required.

### Likelihood Explanation
Requires only that the pool has multiple listed deposit tokens (mainnet pools list several) and dust liquidity in each underlying. The attacker deposits a minimal amount on their own account for each `DepositToken`, then calls `transfer(victim, 1)` 30 times. Cost is bounded by 30 cheap deposits + 30 transfers. The victim cannot prevent receipt (no opt-in), and the attack can be front-run/repeated to re-fill slots as the victim clears them. There is no economic incentive needed — it is griefing, but can be combined with keeping a target position liquidatable for profit. It does, however, require the victim to want to deposit a *new* token type, so impact is highest against active multi-collateral positions approaching liquidation.

### Recommendation
- Only count a deposit token toward `MAX_TOKENS_PER_USER` when the account has a *meaningful* balance (e.g., require a minimum mint amount on deposit, or track tokens only on `deposit`, not on `transfer`/`seize` receipt), or
- Make `transfer`/`seize` not register the recipient's list entry unless the received amount exceeds a dust threshold, or
- Let `addToDepositTokensOfAccount` silently skip (not revert) on transfer-path additions so unsolicited dust cannot occupy slots, while keeping the cap for user-initiated deposits, or
- Allow anyone to call a `sweep`/`remove` helper that force-removes zero- or dust-balance entries from another account's list.

### Proof of Concept
Hardhat-style sketch against deployed pool (assume ≥2 deposit tokens exist; extend loop to 30 to fully saturate):

```ts
// attacker deposits 1 wei of underlying into each DepositToken, then dusts victim
for (const dt of depositTokens /* first N needed to fill victim's list to 30 */) {
  const underlying = await ethers.getContractAt('IERC20', await dt.underlying())
  await underlying.approve(dt.address, 1)
  await dt.deposit(1, attacker.address)          // mints 1 wei share to attacker
  await dt.transfer(victim.address, 1)            // pushes dt into victim's list
}
// victim's depositTokensOfAccount.length + debtTokensOfAccount.length == 30

// victim tries to deposit a collateral type they don't yet hold -> reverts
await newUnderlying.approve(newDepositToken.address, amount)
await expect(
  newDepositToken.connect(victim).deposit(amount, victim.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')
// _mint -> pool.addToDepositTokensOfAccount(victim) hits onlyIfAdditionWillNotReachMaxTokens
```

Key assertions: after the loop, `pool.getDepositTokensOfAccount(victim)` contains all dust tokens; `deposit` for any token not already in the list reverts in `addToDepositTokensOfAccount` (`contracts/Pool.sol:216-220`); the victim must `withdraw`/`transfer` a dust token to zero before regaining a slot, and the attacker can re-fill it with a single `transfer`.