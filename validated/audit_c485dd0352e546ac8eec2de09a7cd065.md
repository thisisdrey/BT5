### Title
Attacker fills victim's `depositTokensOfAccount` list with dust deposits to permanently block new collateral deposits and new debt issuance — (`contracts/Pool.sol`)

### Summary
The Plaza bug class is "unprivileged user inflates a protocol counter to brick a state transition at zero cost." The direct Metronome analog is the per-account token counters `depositTokensOfAccount` / `debtTokensOfAccount`, capped by `MAX_TOKENS_PER_USER = 30` in `contracts/Pool.sol:79`. Anyone can add tokens to a *victim's* list — `DepositToken.deposit(amount_, onBehalfOf_)` lets any caller mint dust collateral positions to an arbitrary `onBehalfOf_` address, and `DepositToken.transfer` lets anyone push dust. Each first receipt calls `pool.addToDepositTokensOfAccount(victim)` (`contracts/DepositToken.sol:486-488`, `517-520`), which reverts with `UserReachedMaxTokens()` once the combined list length reaches 30 (`contracts/Pool.sol:143-148`).

### Finding Description
- `deposit()` accepts any `onBehalfOf_` (`contracts/DepositToken.sol:211-237`); `_mint` registers the token on the beneficiary's account list on first receipt.
- `addToDepositTokensOfAccount` and `addToDebtTokensOfAccount` enforce `debtTokensOfAccount.length + depositTokensOfAccount.length >= MAX_TOKENS_PER_USER → revert` (`contracts/Pool.sol:204-220`, `143-148`).
- Removal only occurs when a token balance goes fully to zero (`_burn`/`_transfer` call `removeFromDepositTokensOfAccount` at `contracts/DepositToken.sol:460-462`, `523-525`), and moving the dust requires `unlockedBalanceOf ≥ amount` (`_revertIfLocked`, `contracts/DepositToken.sol:180-182`, `383-398`). For a victim with an unhealthy or fully-utilized position, `unlockedBalanceOf` returns 0, so the forced dust is non-removable.

Attack path: attacker calls `deposit(1 wei, victim)` on each listed deposit token (or transfers dust) until the victim hits 30 entries. From then on:
- Any `deposit`/`transfer`/`seize` crediting a *new* deposit token to the victim reverts (`_mint`/`_transfer` → `addToDepositTokensOfAccount` → `UserReachedMaxTokens`).
- Any `DebtToken.issue`/`mint` of a synthetic the victim does not already hold reverts the same way via `addToDebtTokensOfAccount`.

### Impact Explanation
- Victim is permanently (while dust is locked) unable to onboard a new collateral type or issue a new synthetic — a liveness/DoS on position management, repeatable against every account at dust cost.
- An underwater victim cannot add a different collateral to restore health; the attack can hold a position liquidatable.
- Existing-token operations still work, so this is bounded griefing rather than theft — analogous in cost and mechanism (free counter inflation) to the Plaza `totalSellReserveAmount` bricking.

### Likelihood Explanation
- Fully unprivileged: only public `deposit`/`transfer` calls; no governor/guardian/oracle dependency.
- Cost is ~30 dust deposits (one wei each of supported underlyings); no capital at risk, since attacker-supplied dust stays in the victim's account.
- Nothing in `SynthContext`, `nonReentrant`, `whenNotPaused`, or supply caps prevents it on deployed configuration; `MAX_TOKENS_PER_USER` is the only gate and the attacker controls the victim's list growth.

### Recommendation
- Enforce the max-tokens check on the *account-initiating* side only for self-directed actions, or drop the counter check from `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` and bound iteration elsewhere.
- Alternatively, restrict `onBehalfOf_` registration (only add to the list on a claim/accept step), or allow anyone to remove dust entries (e.g., a `sweepDust` that burns sub-1-unit balances), so forced dust cannot pin slots.

### Proof of Concept
Hardhat sketch (repo already has `test/DepositToken.test.ts` harness with `poolMock`, `metDepositToken`, and mocked underlyings):

```ts
// 1) Governor lists N deposit tokens; attacker approves underlyings.
// 2) For each deposit token dt_i:
await underlying_i.mint(attacker.address, 1)
await underlying_i.connect(attacker).approve(dt_i.address, 1)
await dt_i.connect(attacker).deposit(1, victim.address) // onBehalfOf = victim
// Repeat across all listed tokens until:
//   debtTokensOfAccount.length(victim) + depositTokensOfAccount.length(victim) == 30

// 3) Victim cannot accept a new collateral type:
await expect(newDt.connect(victim).deposit(amount, victim.address))
  .revertedWithCustomError(pool, 'UserReachedMaxTokens')

// 4) Victim cannot issue a new synthetic:
await expect(newDebtToken.connect(victim).issue(amount, victim.address))
  .revertedWithCustomError(pool, 'UserReachedMaxTokens')

// 5) If victim's position is fully utilized, dust cannot be moved:
await expect(dt_0.connect(victim).transfer(attacker.address, 1))
  .revertedWithCustomError(dt_0, 'NotEnoughFreeBalance')
```

Caveat I could not fully verify in this pass: whether `RewardsDistributor.updateBeforeMintOrBurn` or gateway paths impose additional friction on the dust deposits on a specific live chain; the core mechanism (uncapped attacker-controlled growth of the per-account list plus a hard revert at 30) is confirmed in `Pool.sol`/`DepositToken.sol`.