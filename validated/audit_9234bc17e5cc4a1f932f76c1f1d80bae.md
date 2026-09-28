### Title
Flash-inflated `DebtToken`/`DepositToken` supply dilutes the `RewardsDistributor` index, letting an attacker capture or destroy a full period's unclaimed rewards - (File: contracts/RewardsDistributor.sol)

### Summary
`RewardsDistributor._calculateTokenIndex` accrues reward emissions as `_tokensAccrued = _deltaTimestamps * _speed` and normalizes them by `token_.totalSupply()` at the moment the index is updated (`contracts/RewardsDistributor.sol:201-211`). `RewardsDistributor._calculateTokenDelta` then pays each account `balanceOf(account).wadMul(_deltaIndex)` (`contracts/RewardsDistributor.sol:229-230`). Both `totalSupply` and per-account balance are manipulable inside a single transaction: an attacker can inflate token supply (via `DepositToken.deposit` / `DebtToken.issue` or `DebtToken.flashIssue`, or via `SmartFarmingManager.leverage`) immediately before the index update, absorb a share of the whole elapsed period's emissions proportional to their transient balance, then unwind. This is the direct analog of the Yearn `_reportLoss` bug: a penalty/reward amount computed against a spot, flash-inflatable denominator.

### Finding Description
- The index update path is reachable by anyone: `updateBeforeMintOrBurn` is permissionless ("may be called by anyone") and calls `_updateTokenIndex(token_)` then `_updateTokensAccruedOf(token_, account_)` (`contracts/RewardsDistributor.sol:175-180`). `claimRewards` also drives the same update (`contracts/RewardsDistributor.sol:150-167`).
- `_calculateTokenIndex` uses spot `token_.totalSupply()` with no snapshot/averaging (`contracts/RewardsDistributor.sol:204-206`), so all rewards emitted between `timestamp` and `now` are divided by whatever supply exists at the update instant.
- `_calculateTokenDelta` uses spot `token_.balanceOf(account_)` (`contracts/RewardsDistributor.sol:230`), so an account holding a flash-inflated balance at update time harvests `balance * deltaIndex`.
- Attack trace (unprivileged EOA/contract):
  1. Wait until `_deltaTimestamps * _speed` (accrued but unindexed emissions for `depositToken`/`debtToken`) is large.
  2. Flash-borrow underlying (e.g. Balancer/Aave) → `DepositToken.deposit(amount, attacker)` via `Pool` to mint `msdToken` (and/or `DebtToken.issue`/`flashIssue` to inflate debt-token supply and balance), temporarily becoming a dominant share of `totalSupply`/`balance`.
  3. Call `RewardsDistributor.updateBeforeMintOrBurn(token, attacker)` (public) — index update divides the whole period's emission by the inflated supply; attacker balance is sampled at its flashed size.
  4. Call `RewardsDistributor.claimRewards(attacker)` → `_transferRewardIfEnoughTokens` pays out the accrued `tokensAccruedOf[attacker]` (`contracts/RewardsDistributor.sol:248-256`).
  5. `DepositToken.withdraw` / `DebtToken.repay` and repay the flash loan.
- Mitigation checks: `claimRewards` is `nonReentrant` (`contracts/RewardsDistributor.sol:150`) but the attack uses sequential calls, not reentrancy. No snapshot, time-weighting, or minimum-holding check exists in `RewardsDistributorStorageV2`/index math. Deposit/withdraw fees and issue/repay fees in `FeeProvider` (`contracts/FeeProvider.sol:80-154`) raise attack cost but do not bound it below the emission value being stolen; deposit may also leave locked collateral, which only limits the withdrawable portion, not the balance snapshot taken at step 3.

### Impact Explanation
Theft of unclaimed yield: honest suppliers/borrowers accrued emissions over `_deltaTimestamps`, but the attacker converts a zero-time, flash-funded position into a pro-rata claim on the entire period's emission and withdraws it via `claimRewards`. Alternatively, even without claiming profitably, inflating `totalSupply` permanently shrinks `deltaIndex`, burning rewards that should have accrued to everyone else (direct loss of accrued yield, matching the "incentive/penalty depends on manipulable variable" class).

### Likelihood Explanation
Requires a reward speed > 0 configured on a `DepositToken`/`DebtToken` (governor-set, deployed config), sufficient flash-borrowable liquidity for the underlying, and emission value > fees + flash-loan cost. All entry points (`DepositToken.deposit`, `RewardsDistributor.updateBeforeMintOrBurn`, `claimRewards`, `withdraw`) are public to EOAs/contracts.

### Recommendation
Use a snapshot/averaged supply for index accrual (e.g., cumulative-supply or checkpointed `totalSupply`/`balanceOf` at `timestamp`, not at update time), or settle indexes continuously so flash positions cannot retroactively claim elapsed emissions. At minimum, make `updateBeforeMintOrBurn`/`updateBeforeTransfer` restricted to the token contracts so attacker-chosen update timing can't be combined with a same-tx balance spike — though restricting callers alone does not fix dilution via honest-call ordering.

### Proof of Concept
Hardhat fork sketch — I was unable to fully verify `DebtToken.flashIssue` semantics or deposit lock behavior in this pass, so the PoC below uses plain deposit/withdraw (confirmed public) and should be validated on a fork:

```ts
// Assume distributor has speed > 0 on msdToken and elapsed emissions E.
// 1. Read pending emission: E = (now - tokenStates.timestamp) * speed
// 2. Flash-borrow underlying U (amount >> real TVL share target)
await underlying.approve(depositToken.address, flashAmount)
await depositToken.deposit(flashAmount, attacker.address)   // mint msdToken
// attacker msdToken balance now dominates supply
await rewardsDistributor.updateBeforeMintOrBurn(msdToken.address, attacker.address)
await rewardsDistributor['claimRewards(address)'](attacker.address)
// rewardToken balance of attacker increased by ~ E * flashShare
await depositToken.withdraw(maxUnlocked, attacker.address)  // unwind (unlocked portion)
// repay flash loan with claimed rewards + withdrawn underlying
```

Expected on a mainnet fork against pool `Pool`/distributor with active speed: attacker reward > 0 despite zero holding duration; per-token `deltaIndex` permanently reduced versus the no-attack baseline, reducing all other users' `claimable`.

*Caveat: if `flashIssue` exists as a same-tx repayable mint, it lowers required collateral further; I could not read `DebtToken.flashIssue`/`DepositToken` deposit-lock internals within available iterations — confirm on fork that the inflated balance is sampled by `balanceOf` before unwind.*