### Title
RewardsDistributor accrues rewards on interest-grown `balanceOf` retroactively, letting debt holders over-claim rewards — (File: contracts/RewardsDistributor.sol)

### Summary
The SQLite bug (CVE-2020-13871) is a use-after-free caused by a rewrite happening too late: an object is consumed after it should have been transformed. The Metronome analog is in `RewardsDistributor._calculateTokenDelta`: the reward delta `balanceOf(account) * (currentIndex - accountIndex)` is computed with the *current* balance applied retroactively over the entire index delta. For `DebtToken`, `balanceOf` grows continuously with accrued interest, but the reward index/accounting is not "rewritten" (per-account index + balance snapshot) when interest accrues. A borrower therefore earns rewards for the whole index window as if they had held the grown balance the entire time — an inflation of unclaimed yield at other users' expense.

### Finding Description
In `contracts/RewardsDistributor.sol`, `_calculateTokenDelta` computes `_tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex)` using the live balance at claim/update time, multiplied by the full index delta since `accountIndexOf[token][account]` was last written. For a `DebtToken`, `balanceOf` (contracts/DebtToken.sol) returns `principalOf * currentDebtIndex / debtIndexOf[account]` — it increases every second with interest, without any checkpoint in the distributor. The hooks `updateBeforeMintOrBurn` and `updateBeforeTransfer` only fire on explicit mint/burn/transfer, not on passive interest accrual.

Attack path (unprivileged EOA):
1. Deposit collateral via `DepositToken.deposit`, then `DebtToken.issue` to open a debt position (this sets `accountIndexOf` to the current index via `_mint`'s `updateRewardsBeforeMintOrBurn`).
2. Wait. The debt balance grows via interest; the reward index also grows.
3. Call `RewardsDistributor.claimRewards(attacker)` (public, only `nonReentrant`). `_updateTokensAccruedOf` credits `balanceOf(now) * deltaIndex` — including the interest growth applied retroactively over the whole window.
4. Repeat: `repayAll`, re-`issue` to reset `accountIndexOf`, keep farming the systematic over-accrual.

No modifier stops this: `claimRewards` is public and `nonReentrant` only; `updateBeforeMintOrBurn` is permissionless; nothing forces per-interest-accounting checkpointing.

### Impact Explanation
Theft of unclaimed yield. The over-accrued `tokensAccruedOf` is paid out of the distributor's reward token balance in `_transferRewardIfEnoughTokens`, diluting legitimate claimers. The magnitude is `balance_growth_fraction * normal_reward` per period — with a nonzero debt `interestRate`, an attacker holding a large debt position extracts rewards on phantom balance. When the distributor's reward token balance is insufficient, honest users' claims silently no-op (`_transferRewardIfEnoughTokens` just skips the transfer), so the attacker can drain available rewards first.

### Likelihood Explanation
Requires only a standard deposit + issue + claim sequence with public entry points and an active `tokenSpeeds` on a DebtToken. Profitability depends on `interestRate` vs reward speed, but the accounting error exists whenever `interestRate > 0`. The one mitigating factor: the over-accrual is proportional to interest growth (typically small per period), so this is a reward-dilution/leak rather than instant draining — repeatable but bounded by the interest term.

### Recommendation
Checkpoint the account's reward index at each `accrueInterest`/`balanceOf`-changing boundary, or base `_tokensDelta` on a snapshotted principal rather than live `balanceOf` (e.g., use `principalOf` for DebtToken via a dedicated interface, or call `updateBeforeMintOrBurn` from within `DebtToken.accrueInterest` for all debt holders — infeasible; prefer principal-based accounting). At minimum, document and bound the drift.

### Proof of Concept
Hardhat sketch (fork or local deployment):

```ts
// setup: pool, depositToken (msdMET), debtToken (msUSDDebt), rewardsDistributor with speed on msUSDDebt
await met.mint(attacker.address, depositAmount)
await met.connect(attacker).approve(msdMET.address, MaxUint256)
await msdMET.connect(attacker).deposit(depositAmount, attacker.address)
await msUSDDebt.connect(attacker).issue(debtAmount, attacker.address) // sets accountIndexOf

await msUSDDebt.connect(governor).updateInterestRate(parseEther('0.5')) // 50% APR
await time.increase(time.duration.years(1))

// At claim, balanceOf(attacker) ≈ 1.5 * debtAmount but deltaIndex covers the whole year
const accruedView = await rewardsDistributor.claimable(attacker.address)
await rewardsDistributor.claimRewards(attacker.address)

// Expected: reward ≈ debtAmount * deltaIndex (time-weighted)
// Actual:   reward ≈ 1.5 * debtAmount * deltaIndex — ~50% over-claim
const claimed = await rewardToken.balanceOf(attacker.address)
expect(claimed).to.be.closeTo(expected.mul(15).div(10), tolerance)
```

Compare against a control account with `interestRate = 0`; the delta between the two claims (net of reward-speed differences) isolates the retroactive-interest inflation.

Note: I verified `RewardsDistributor`, `DebtToken`, and their hooks directly; I did not fully confirm which tokens have nonzero `tokenSpeeds` on the deployed configurations or whether `DepositToken.balanceOf` is also interest-growing (which would extend the same flaw to suppliers). The core accounting defect — applying end-of-period `balanceOf` over the whole index delta — is confirmed in code.