### Title
Retroactive reward weighting: interest-grown debt balances earn rewards for periods when the balance was smaller - (File: contracts/RewardsDistributor.sol)

### Summary
`RewardsDistributor._calculateTokenDelta` computes an account's pending rewards as `token_.balanceOf(account_).wadMul(_deltaIndex)`, where `_deltaIndex` spans the entire period since the account's last `accountIndexOf` checkpoint. The index math assumes `balanceOf` is constant between hook calls (`updateBeforeMintOrBurn` / `updateBeforeTransfer`). That assumption holds for `DepositToken`, but it does **not** hold for `DebtToken`: `DebtToken.balanceOf` grows continuously with accrued interest (`principalOf * debtIndex`), and interest accrual is not accompanied by any `updateBeforeMintOrBurn` call. A borrower who never touches their position therefore has all past reward deltas weighted at the inflated, interest-grown balance — exactly the "rewards weighted at a higher level than reality" class from the report — and drains more than their pro-rata share of the reward stream.

### Finding Description
In `RewardsDistributor._calculateTokenDelta` (contracts/RewardsDistributor.sol:217-231), the account's reward delta is `balanceOf * (currentIndex - accountIndex)`. Checkpoints (`accountIndexOf`) are only advanced when `updateBeforeMintOrBurn`/`updateBeforeTransfer` run, i.e., on `issue`, `repay`, `flashIssue`, `seize`-adjacent burns, etc. `DebtToken.balanceOf`, however, is interest-bearing: it returns principal scaled by the ever-growing `debtIndex`, so a static borrower's balance increases every second without any hook firing.

Concretely: `userA` issues debt at time t0. Rewards accrue per second on the debt token (`tokenSpeeds[debtToken] > 0`). At t1, `userA`'s `balanceOf` has grown by accrued interest `I`. When `userA` next calls `repay`/`issue` (or anyone calls `claimRewards(userA)`), `_updateTokensAccruedOf` credits `(balance0 + I) * deltaIndex`, even though the correct time-weighted accrual only earned interest progressively — the early portion of the window should have been weighted at `balance0`. The excess is paid from the same reward pool that honest, regularly-checkpointed users draw from, so it is a direct dilution/theft of unclaimed yield. Symmetrically, the same mechanism also means a `DepositToken` path that mutates balance without a hook (e.g., any mint/burn site missing `updateBeforeMintOrBurn`) would produce the same stale-weight accounting; the verified reachable instance is the interest-bearing `DebtToken` balance.

### Impact Explanation
Theft/dilution of unclaimed yield: passive borrowers are overpaid in `rewardToken` relative to the intended per-second pro-rata emission, reducing what is left for every other reward recipient. The overpayment grows with the interest rate and the length of the checkpoint gap, and is unbounded in time.

### Likelihood Explanation
Reachable by any unprivileged borrower: open a debt position via `Pool`/`DebtToken.issue`, then simply do nothing (or deliberately avoid any transaction that would trigger `updateBeforeMintOrBurn`) while interest accrues, then claim. No privileged role, no oracle manipulation, no reentrancy required. Rewards on debt tokens are a supported configuration (`onlyIfTokenExists` admits both deposit and debt tokens).

### Recommendation
Either exclude `DebtToken` from reward distribution, or base the delta on a checkpointed principal snapshot rather than live `balanceOf` — e.g., store the balance at checkpoint time alongside `accountIndexOf`, or have `DebtToken` expose a non-interest-adjusted balance for reward accounting. Alternatively, time-weight the accrual by integrating `balanceOf` changes caused by `debtIndex` growth into the index calculation.

### Proof of Concept
Hardhat sketch (fork or local deployment with `updateTokenSpeed(debtToken, speed)`):

```ts
// speed = 1 reward/sec on debtToken; alice borrows 1000, debtIndex grows 10%/yr
await debtToken.issue(alice, parseEther('1000')); // checkpoint: accountIndex = idx0
await time.increase(365 * 24 * 3600);
// alice's balanceOf is now ~1100 due to accrued interest
const claimableAlice = await rewardsDistributor['claimable(address)'](alice.address);
// claimableAlice ≈ 1100 * deltaIndex, but correct weighting is
// ∫ balance(t) dt which is < 1100 * deltaIndex (balance only reached 1100 at the end)
// A second borrower bob who repays/re-issues frequently checkpoints at lower
// balances and accrues strictly less per unit of debt-time than alice.
```

Caveat: I confirmed the delta formula and hook-based checkpointing in `RewardsDistributor.sol` and `DebtToken`'s interest-bearing `balanceOf` via index snippets; I could not re-verify every `DepositToken` burn/mint site (e.g., `seize`) for missing hooks due to limited iterations — if `seize` skips `updateBeforeMintOrBurn`, the same stale-balance accounting applies there too (under/over-payment for liquidated users).