### Title
DebtToken interest-grown balance is multiplied over the full reward-index delta, letting an attacker over-accrue and drain unclaimed rewards — (File: contracts/RewardsDistributor.sol)

### Summary
`RewardsDistributor._calculateTokenDelta` computes an account's reward delta as `token_.balanceOf(account_).wadMul(_deltaIndex)`, using the *current* token balance against the entire index delta elapsed since the account's last accrual. For `DebtToken`, `balanceOf` is a compounding, interest-accruing value (`principal * debtIndex`), not a static share balance. An attacker who lets their debt grow without triggering `updateBeforeMintOrBurn`/`updateBeforeTransfer` is credited rewards on the grown balance for the whole elapsed window — the reward-accounting equivalent of a use-after-free: a stale index checkpoint combined with a mutated underlying balance. `claimRewards` is permissionless (anyone may claim for any account) and pays out whenever the distributor's reward-token balance is sufficient.

### Finding Description
- `RewardsDistributor._calculateTokenDelta` (contracts/RewardsDistributor.sol:217-231):
  ```solidity
  uint256 _deltaIndex = _tokenIndex - _accountIndex;
  _tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
  ```
  The delta assumes `balanceOf` was constant across `[_accountIndex, _tokenIndex]`. That holds for `DepositToken` shares but not for `DebtToken`, whose `balanceOf` grows with the pool debt index every block.
- `updateBeforeMintOrBurn` is explicitly callable by anyone ("This function also may be called by anyone"), so the attacker controls when their checkpoint is refreshed (contracts/RewardsDistributor.sol:175-180).
- `claimRewards(address[],IERC20[])` loops tokens, updates the index, calls `_updateTokensAccruedOf`, then `_transferRewardIfEnoughTokens` pays `tokensAccruedOf[account_]` if the contract's `rewardToken` balance covers it (contracts/RewardsDistributor.sol:150-168, 248-256).
- Attack path (unprivileged EOA only):
  1. Deposit collateral, mint debt via `DebtToken.issue` so the account's `accountIndexOf[debtToken]` is checkpointed at `INITIAL_INDEX`.
  2. Take no action that touches rewards for a long period while `balanceOf` compounds (debt interest accrual is automatic).
  3. Call `claimRewards(attacker)` (or `updateBeforeMintOrBurn` then claim). `_tokensDelta` = grownBalance × full deltaIndex, over-crediting all interest growth for the whole window.
- Mirror-image loss for others: the over-crediting drains `rewardToken` held by the distributor, so later honest claims hit the `amount_ <= _balance` check and are silently skipped — other users' accrued yield is effectively stolen.

### Impact Explanation
Theft of unclaimed yield / draining of the reward pool. The attacker receives reward tokens proportional to a balance they did not hold for the measured period (interest that accrued *after* the rewards were earned). Every excess token comes from the distributor's `rewardToken` balance, which is shared across all claimants; once drained, `_transferRewardIfEnoughTokens` silently pays nothing to legitimate users (`amount_ <= _balance` fails, no revert). This matches the CVE class: state freed/mutated (debt index grown) but consumed through a stale reference (old account checkpoint) to produce an invalid result with fund loss.

### Likelihood Explanation
- Requires only ordinary user actions: a deposit and a mint — both permissionless public entry points.
- No privileged role, no oracle manipulation, no flash loan needed; time and interest accrual do the work.
- The over-credit factor equals the debt-index growth between checkpoints; on pools with material borrow APR and long idle periods, the inflation is significant. The account must simply avoid (or itself trigger) reward updates — achievable since the attacker controls the only calls that checkpoint its index.
- `nonReentrant`, pause flags, and `SynthContext` do not mitigate: all calls are legitimate, single-transaction, top-level calls.

### Recommendation
Fix `_calculateTokenDelta`/`_updateTokensAccruedOf` so the per-token weight used is the balance that was actually held over the index interval:
- For interest-bearing tokens, store the account's *principal* (or the balance snapshot taken at the last index update) in `RewardsDistributorStorage` at accrual time and compute deltas from that stored snapshot, or
- Have `DebtToken` expose a non-growing principal (`principalOf`) and use it as the reward weight for debt tokens, since rewarding debt-holding is already dubious; alternatively exclude `DebtToken` from `onlyIfTokenExists` reward tracking entirely, or
- Checkpoint per-token balances inside the distributor (snapshot `balanceOf` when the index is updated) rather than re-reading a mutable external balance against a stale index.

### Proof of Concept
Foundry-style outline (fork or local deploy of Pool + DebtToken + RewardsDistributor with a nonzero `tokenSpeeds[debtToken]`):

```solidity
// Setup: pool with msUSD debt token, rewardsDistributor with speed>0 on debtToken,
// distributor funded with rewardToken.
uint256 debt = 100_000e18;
depositToken.deposit(collateral, alice);          // alice = attacker EOA
debtToken.issue(debt, alice);                     // accountIndexOf[debt][alice] = INITIAL_INDEX

// Idle: no call touching alice's index. Debt balance compounds.
vm.warp(block.timestamp + 180 days);
// alice balanceOf grew from debt to debt * (indexNow/indexAtIssue)

// Baseline comparison: bob did the same but calls updateBeforeMintOrBurn each block
uint256 aliceClaim = distributor.claimable(alice);   // grownBalance * fullDeltaIndex
uint256 bobClaim   = distributor.claimable(bob);     // correct per-interval accrual
assertGt(aliceClaim, bobClaim);                       // over-credit ∝ interest accrued

distributor.claimRewards(alice);                      // pays out, drains rewardToken
// Subsequent honest claims get nothing (amount_ > balance → silent skip)
```

The invariant broken is reward-accrual conservation: total `tokensAccruedOf` exceeds `speed × time` distributed across actual holdings.