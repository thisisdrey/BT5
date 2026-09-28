### Title
RewardsDistributor attributes rewards using the current interest-inflated `DebtToken.balanceOf` instead of the historical balance during the accrual period - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
`RewardsDistributor` computes a user's reward delta as `token_.balanceOf(account_) * deltaIndex` (`_calculateTokenDelta`, contracts/RewardsDistributor.sol:217-231). For `DebtToken`, `balanceOf` is not a static share balance — it returns `principal * debtIndex / debtIndexOf[account]`, i.e. principal plus all interest accrued up to `block.timestamp` (contracts/DebtToken.sol:196-206). When an account's `accountIndexOf` is stale (because it hasn't minted/burned since the last index update), the entire index delta covering that whole elapsed period is multiplied by today's inflated balance. This is the same bug class as the reference report: a *current* state (balance now, like `owner()` now) is applied retroactively to a past accrual period, instead of the balance the account actually held during that period.

### Finding Description
- `updateBeforeMintOrBurn` / `updateBeforeTransfer` only run when the account mints, burns, repays or transfers (contracts/DebtToken.sol:114-121, 525, 577). Passive balance growth through `accrueInterest` never touches the rewards index.
- `_updateTokensAccruedOf` → `_calculateTokenDelta` multiplies the *current* `balanceOf` (principal + all accrued interest) by `_tokenIndex - accountIndex`, attributing that inflated balance to the entire interval since the account's last index update.
- Because reward emission is fixed (`deltaTime * speed`), over-crediting one account comes directly out of the fixed reward pool. Other borrowers who touch their position during the period (triggering index updates with smaller balances) and depositors are systematically underpaid; once `rewardToken` reserves are drained, `_transferRewardIfEnoughTokens` silently skips their claims (contracts/RewardsDistributor.sol:248-256) — permanent loss of unclaimed yield.
- An unprivileged attacker maximizes extraction by borrowing once at the start of a reward period and never interacting again until claim time; their account index stays stale, and they are credited on `principal * (1 + interestRate * T / YEAR)` for the whole `T`, not the time-weighted average.

### Impact Explanation
Theft of unclaimed yield / unfair reward distribution: borrowers who keep their rewards index stale are credited rewards on interest that had not yet accrued during the measured window. The extra tokensAccrued are paid from a fixed emission pool, so they dilute and can permanently strip honest users' rewards — when the contract's `rewardToken` balance is insufficient, `_transferRewardIfEnoughTokens` pays nothing and the deficit is socialized onto late claimers.

### Likelihood Explanation
Requires only that the governor has set a nonzero `tokenSpeeds` for a `DebtToken` (supported configuration — `onlyIfTokenExists` explicitly allows debt tokens, contracts/RewardsDistributor.sol:91-97) and a nonzero `interestRate`. The attacker only needs to call `DebtToken.issue` once and later `claimRewards` — no privileged roles, no manipulation, no oracle dependence. Interest-accruing debt tokens are the normal operating mode of the protocol.

### Recommendation
Do not reward `DebtToken` balances that grow passively, or snapshot account balances per index window. Options: reward only `principalOf` (static shares) rather than `balanceOf` (principal + accrued interest), by having `DebtToken` expose a rewards-specific balance function used by `RewardsDistributor`; or periodically checkpoint `accountIndexOf`/`balanceOf` pairs so each index interval is multiplied by the balance actually held during that interval.

### Proof of Concept
Foundry-style PoC against a pool with an active `RewardsDistributor` whose `rewardToken` is funded and `tokenSpeeds[debtToken] = speed > 0`, `debtToken.interestRate > 0`:

```solidity
// Assume alice and bob each issue the same debt principal P at t0.
// Their accountIndexOf[debtToken] is set to the index at t0.
debtToken.issue via pool for alice and bob  // principalOf = P each

// Warp a long period T (e.g. 180 days) with no interactions from alice.
vm.warp(t0 + T);

// Bob interacts mid-period (e.g. small repay), which calls
// updateBeforeMintOrBurn -> _updateTokensAccruedOf with bob's
// balance at that time, freezing his accrual at a smaller base.
debtToken.repay(bob, dust);

// Alice never touched her position. At claim time:
//   alice delta = balanceOf(alice) * (index_now - index_t0)
//               = P * debtIndex_now / debtIndex_t0 * deltaIndex
// Her inflated balance (principal + T days of interest) is applied
// to the whole deltaIndex.
rewardsDistributor.claimRewards(alice, tokens);
rewardsDistributor.claimRewards(bob, tokens);

uint256 a = rewardToken.balanceOf(alice);
uint256 b = rewardToken.balanceOf(bob);
// alice receives strictly more than bob despite identical principal
// and identical holding time; total tokensAccrued exceeds
// speed * T, so late claimers can be left with zero payout.
assertGt(a, b);
```

Both accounts accrued rewards over the same window with equal principal, but Alice's accrued interest is counted as if held for the full period, while Bob's index was checkpointed mid-period at a lower balance. The reward conservation invariant (`sum(tokensAccruedOf) <= speed * elapsedTime`) is broken.