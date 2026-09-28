### Title
RewardsDistributor credits rewards on interest-accrued debt balance for past periods - (File: contracts/RewardsDistributor.sol)

### Summary
`RewardsDistributor._calculateTokenDelta()` computes a user's reward delta as `token_.balanceOf(account_) * deltaIndex`, where `deltaIndex` spans the period since the user's `accountIndexOf` was last updated. For `DebtToken`, `balanceOf` is not a static stored balance — it returns `principal * currentDebtIndex / debtIndexOf[account]`, i.e. principal plus all interest accrued up to the current block (`DebtToken.sol:196-206`). Rewards for a `DebtToken` therefore get computed on an interest-inflated end-of-period balance, as if the user had held that inflated debt for the entire elapsed period. This misattributes emission rewards to borrowers whose debt grew only through interest, diluting every other claimant. It is the same bug class as the reported trove-manager flaw: the reward calculation mixes a snapshot index with a denominator/numerator that silently includes pending accrual not present during the indexed period.

### Finding Description
The index accounting in `RewardsDistributor` is standard Compound-style: `_calculateTokenIndex` accrues `speed * dt / totalSupply` into a global per-token index (`RewardsDistributor.sol:197-212`), and `_calculateTokenDelta` pays `balanceOf * (newIndex - accountIndex)` (`RewardsDistributor.sol:217-231`).

For `DepositToken` this is correct because `balanceOf` is a plain stored value and every mutation (`_mint`, `_burn`, `_transfer`) is wrapped by `updateRewardsBeforeMintOrBurn`/`updateRewardsBeforeTransfer` (`DepositToken.sol:124-144, 444, 469-472, 498-502`), so the balance used always matches the balance actually held during the indexed window.

For `DebtToken` this invariant breaks. `DebtToken.balanceOf` recomputes `principal * projectedDebtIndex / debtIndexOf[account]` at call time (`DebtToken.sol:196-206`), and `projectedDebtIndex` keeps growing between `accrueInterest()` writes (`DebtToken.sol:551-566`). A borrower's `principalOf`/`debtIndexOf` are only re-snapshotted inside `_mint`/`_burn` (`DebtToken.sol:525-535, 590-594`). A borrower who never calls `issue`, `repay`, `repayAll`, or gets liquidated has a stale `accountIndexOf` in the distributor, yet when rewards are finally updated (by their own action, by a permissionless `updateBeforeMintOrBurn` call from anyone per `RewardsDistributor.sol:173-180`, or by `claimRewards(accounts_, ...)` which accepts arbitrary accounts at `RewardsDistributor.sol:150-167`) the delta is computed against the current, interest-grown `balanceOf`.

Concretely: if `deltaIndex` covers time `[t0, t1]` and the borrower's debt grew by factor `k = debtIndex(t1)/debtIndex(t0) > 1` from interest, they are paid `principal * k * deltaIndex` instead of approximately `principal * avg(debtIndex growth) * deltaIndex`. The over-credit equals `principal * (k - 1) * deltaIndex` plus the compounding path difference — i.e. they earn rewards on interest that did not exist during most of the accrual window. Symmetrically, `totalSupply()` used as the index denominator also includes pending interest (`DebtToken.sol:500-503`), so the inflation is internally consistent only for the aggregate, not for the per-account retroactive window.

### Impact Explanation
Unprivileged theft of unclaimed yield. Any borrower of a `DebtToken` that has `tokenSpeeds[debtToken] > 0` (borrower incentive distributions are a supported, deployed configuration — the distributor explicitly supports both `DepositToken` and `DebtToken` via `onlyIfTokenExists`, `RewardsDistributor.sol:91-97`) accrues more reward tokens than their time-weighted debt share entitles them to. The excess comes directly out of the fixed emission stream (`speed` per second), so every other supplier/borrower claimant is diluted by exactly the over-credited amount, and the reward token balance in the distributor is drained faster than intended. The exploit requires only: (1) open a debt position early, (2) do nothing while interest compounds, (3) claim — or simply wait for anyone to permissionlessly trigger `updateBeforeMintOrBurn`/`claimRewards` on the attacker's behalf. No privileged role, oracle manipulation, or timing dependency is needed.

### Likelihood Explanation
High whenever borrower rewards are enabled and interest rates are nonzero. The inflation factor `k` grows continuously with `interestRate` and elapsed time; long-lived debt positions (common for leverage users via `SmartFarmingManager`) accumulate a material gap between principal and `balanceOf`. The attack is passive — no capital risk, no racing, no reversion paths. The `updateBeforeMintOrBurn` hooks in `DebtToken._mint`/`_burn` do not help because they fire at action time and re-snapshot the account's debt index *after* the distributor has already consumed the inflated `balanceOf`. Guards (`nonReentrant`, `whenNotShutdown`, `onlyIfSyntheticTokenExists`) are irrelevant to the accrual path.

### Recommendation
Do not use the live, interest-including `balanceOf` as the reward base for `DebtToken`. Options:
- Base borrower rewards on `principalOf[account]` (the stored debt share) instead of `balanceOf`, matching how `DepositToken` uses a static stored balance. Expose a principal getter and have `RewardsDistributor._calculateTokenDelta` use it when `token_` is a `DebtToken`.
- Alternatively, re-snapshot `principalOf`/`debtIndexOf` (a "settle interest" step) inside `updateBeforeMintOrBurn`/`updateBeforeTransfer` before the distributor reads the balance, so the reward base equals the balance actually carried through the indexed window.

### Proof of Concept
Hardhat test sketch against the deployed-contract logic:

```ts
// Setup: pool, depositToken, debtToken, rewardsDistributor with rewardToken (e.g. esMET/VSP)
// governor: rewardsDistributor.updateTokenSpeed(debtToken, speed)
// alice deposits collateral, issues 100_000 synths of debt; bob does the same.

// 1. t0: alice issues debt -> updateBeforeMintOrBurn(alice) sets accountIndexOf
//    principalOf[alice] = 100_000, debtIndexOf[alice] = debtIndex(t0)

// 2. Bob repays fully and exits (principalOf[bob] = 0), removing himself as claimant.

// 3. Advance time: interest accrues so debtIndex(t1)/debtIndex(t0) = 1.10
//    alice's balanceOf now = 110_000 though principalOf = 100_000.
//    No DebtToken mint/burn touches alice in this window.

// 4. Anyone calls rewardsDistributor.updateBeforeMintOrBurn(debtToken, alice)
//    or rewardsDistributor.claimRewards([alice], [debtToken]).
//    _calculateTokenDelta computes aliceDelta = 110_000 * deltaIndex,
//    but the correct time-weighted base over [t0, t1] was < 110_000
//    (it grew continuously from 100_000). Over-credit ≈ 100_000 * (k-1) * deltaIndex.

// 5. Assert: tokensAccruedOf[alice] > 100_000 * deltaIndex (the under-estimate bound),
//    and total claimed rewards across accounts exceed the intended pro-rata share.
```

Expected outcome: `tokensAccruedOf[alice]` reflects a balance that only existed at `t1` applied to the whole `[t0, t1]` window, demonstrating reward inflation proportional to accrued interest, reproducible on a mainnet fork against any pool with a non-zero borrower `tokenSpeed`.