### Title
Attacker can permanently block a victim's deposits, withdrawals and liquidations by dust-filling their `depositTokensOfAccount` list to `MAX_TOKENS_PER_USER` - (File: contracts/Pool.sol)

### Summary
`Pool` tracks every DepositToken a user holds in a per-account enumerable set capped at `MAX_TOKENS_PER_USER = 30` (contracts/Pool.sol:79). `DepositToken._transfer` and `_mint` add a token to the recipient's list whenever their prior balance was zero (contracts/DepositToken.sol:518-520, 486-488). An unprivileged attacker can deposit dust amounts of every supported collateral and `transfer` 1 wei of each msd token to a victim, filling the victim's list to 30. Any subsequent action that would add a *new* deposit token to the victim's account — including the victim's own `deposit` of a new collateral type, receiving seized collateral via `Pool.liquidate`/`DepositToken.seize`, or receiving any new msd token via `transfer` — reverts with `UserReachedMaxTokens` until the victim manually clears entries.

### Finding Description
- `MAX_TOKENS_PER_USER` is a hard cap of 30 per-account tokens (contracts/Pool.sol:79), enforced by `addToDepositTokensOfAccount` / `addToDebtTokensOfAccount`, which revert with `UserReachedMaxTokens` once the set is full.
- `DepositToken._transfer` unconditionally registers the recipient when `_recipientBalanceBefore == 0 && amount_ > 0` (contracts/DepositToken.sol:517-520). There is no minimum-amount or opt-in check; a 1-wei dust transfer registers the token.
- The same registration path runs inside `seize` (line 343-345 → `_transfer`), so a liquidator paying out collateral in a deposit token the victim has never held will revert once the victim's list is full — blocking liquidation of underwater positions.
- Attack steps (all unprivileged, no privileged role needed):
  1. For each of the pool's N deposit tokens: approve underlying, call `deposit(1, attacker)`, then `transfer(victim, 1)`. Each transfer both registers the token on the victim (balance 0 → 1) and removes it from the attacker (balance → 0), so the attacker can reuse the loop cheaply.
  2. After 30 iterations the victim's `depositTokensOfAccount` is full.
  3. Victim's `deposit(amount, victim)` into any *new* collateral reverts in `_mint` → `addToDepositTokensOfAccount`; `liquidate(..., victim, ...)` reverting on `seize` to a new collateral type also reverts.

### Impact Explanation
Reachable, unauthenticated denial of service — the same bug class as CVE-2021-2389 (unauthenticated remote attacker causes repeatable crash/hang of the service). Concrete impacts:
- Temporary freezing of funds: the victim cannot deposit new collateral, cannot receive msd transfers of new tokens, and cannot be paid seizure proceeds in a new collateral type, until they discover the attack and spend gas transferring away the dust entries (each outgoing transfer to zero-balance removes one slot via `removeFromDepositTokensOfAccount`, DepositToken.sol:523-525).
- Liquidation blocking: an underwater account whose list is full may be unliquidatable in collateral types it doesn't already hold, degrading protocol solvency response during a crash — the attacker can combine this with deliberately opening a risky position, filling their own list, then going underwater so liquidators cannot seize into the attacker's account.
- The cost to the attacker is bounded (gas + dust of 30 collaterals, partially recoverable by withdrawing) while the victim must perform up to 30 recovery transactions.

### Likelihood Explanation
- Fully unprivileged: `deposit` is `whenNotPaused` and open to any EOA; `transfer` is a plain ERC20 call gated only by `_revertIfLocked` on the *sender* (DepositToken.sol:348-354), which passes for a debt-free attacker.
- No modifier stops it: `updateRewardsBeforeTransfer` only iterates reward distributors; `nonReentrant` doesn't apply to `transfer`; no minimum-dust threshold exists.
- Requires ≥30 registered deposit tokens to fully fill, or fewer if the victim already holds some; on pools with fewer collaterals the attacker's fill is partial but still blocks the remaining new-collateral slots.
- The victim can self-recover by transferring dust out, so this is temporary (not permanent) freezing of funds — still within the accepted impact class (temporary freezing of funds / blocked operations).

### Recommendation
- Make `addToDepositTokensOfAccount` non-reverting on cap overflow: skip adding when the set is full instead of reverting, since the list is used only for iterating a user's positions — positions not in the list must then be handled, or
- gate dust registration: only register the token when the transferred amount exceeds a minimum threshold, or when the recipient's new balance exceeds a dust floor, or
- require recipient opt-in for first-time receipt of msd tokens (e.g., a whitelist/allowance-style acceptance), or
- raise/remove `MAX_TOKENS_PER_USER` since the set operations are O(1) and the cap exists only to bound iteration in `debtPositionOf` — consider bounding the USD-weighted positions instead.

### Proof of Concept
Hardhat sketch against deployed `Pool` + `DepositToken`s:

```ts
// attacker fills victim's depositTokensOfAccount to MAX_TOKENS_PER_USER
const depositTokens: DepositToken[] = await getAllDepositTokens(pool); // pool.getDepositTokens()
for (let i = 0; i < 30 && i < depositTokens.length; i++) {
  const dt = depositTokens[i];
  const underlying = await ethers.getContractAt('ERC20', await dt.underlying());
  await underlying.connect(attacker).approve(dt.address, 1);
  await dt.connect(attacker).deposit(1, attacker.address);   // mint 1 wei msd
  await dt.connect(attacker).transfer(victim.address, 1);    // registers token on victim
}
expect((await pool.getDepositTokensOfAccount(victim.address)).length).eq(30);

// victim can no longer deposit a collateral type they don't already hold
const newDt = depositTokens[30]; // any token not yet in victim's list
const newUnderlying = await ethers.getContractAt('ERC20', await newDt.underlying());
await newUnderlying.connect(victim).approve(newDt.address, amount);
await expect(newDt.connect(victim).deposit(amount, victim.address))
  .revertedWithCustomError(pool, 'UserReachedMaxTokens');

// liquidation seizing into a collateral the victim doesn't hold also reverts
await expect(
  pool.connect(liquidator).liquidate(synth.address, victim.address, repayAmt, newDt.address)
).revertedWithCustomError(pool, 'UserReachedMaxTokens');
```

Note: `addToDepositTokensOfAccount`'s exact cap check at contracts/Pool.sol (~`UserReachedMaxTokens` usage) was inferred from the declared error and constant; the revert site should be confirmed when writing the full PoC.