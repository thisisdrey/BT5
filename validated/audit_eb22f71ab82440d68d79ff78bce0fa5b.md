### Title
Reward token dust is permanently locked in `RewardsDistributor` — truncated accrual, no sweep/recovery path — ([File: contracts/RewardsDistributor.sol])

### Summary
`RewardsDistributor` distributes a `rewardToken` to deposit/borrow token holders via a reward index. Rewards accrue through integer-truncated `wadMul`/`wadDiv` math, and once a campaign's `tokenSpeeds` is set to `0`, any leftover `rewardToken` balance — including per-user dust entitlements too small to be worth claiming and the residual created by index truncation — has no exit path: there is no sweep/recover function on the contract, and `_transferRewardIfEnoughTokens` is the only code path that moves `rewardToken` out.

### Finding Description
Two compounding properties in `contracts/RewardsDistributor.sol` guarantee permanently stranded reward tokens:

1. **Truncated accrual.** `_calculateTokenIndex` computes `_tokensAccrued.wadDiv(_totalSupply)` and `_calculateTokenDelta` computes `balance.wadMul(_deltaIndex)` (lines 205-206, 230). Both round down, so the sum of all users' `tokensAccruedOf` entitlements is strictly less than the tokens accounted for by the index growth. The rounding residual accumulates as contract balance that maps to no claim.

2. **Claims are the only exit, and they can silently no-op.** `_transferRewardIfEnoughTokens` (lines 248-256) is the sole function that transfers `rewardToken` out. It only transfers when `amount_ <= balance`; any residual balance smaller than the smallest accrued claim can never leave. The contract inherits `Manageable`, `ReentrancyGuardDeprecated`, and `ReentrancyGuardTransient` (lines 39-45) — it does not inherit `TokenHolder` or expose any governor/keeper sweep, so after `updateTokenSpeed` sets all speeds to `0` (line 315), remaining `rewardToken` is frozen forever. This matches the referenced bug class: dust accrues from truncation and sub-gas claims, and no admin path exists to recover it.

### Impact Explanation
Permanent freezing of funds: after a rewards campaign ends, leftover `rewardToken` (rounding dust plus unclaimable dust entitlements) is locked in the `RewardsDistributor` contract with no withdrawal mechanism short of a contract upgrade.

### Likelihood Explanation
Certain in the long run. The `wadDiv`/`wadMul` truncation produces a nonzero residual on essentially every campaign with non-trivial supply, and users with small positions regularly accrue sub-gas-cost entitlements that are never claimed. Setting all speeds to zero (campaign end) is a normal governance operation.

### Recommendation
Add a `onlyGovernor` recovery function (e.g., reuse the `TokenHolder.sweep` pattern used elsewhere in the codebase) allowing the governor to withdraw `rewardToken` after all `tokenSpeeds` have been zero for a defined grace period, or allow sweeping any non-reward token immediately.

### Proof of Concept
Foundry/Hardhat fork sketch:

```solidity
// 1. Governor sets a speed and funds the distributor
rewards.updateTokenSpeed(depositToken, 1 ether);       // tokenSpeeds > 0
rewardToken.transfer(address(rewards), 1000 ether);    // funded

// 2. A user holds deposit tokens; time passes
vm.warp(block.timestamp + 30 days);

// 3. User claims; wadMul/wadDiv truncation leaves residual
rewards.claimRewards(user);                             // user receives < funded amount

// 4. Campaign ends
vm.prank(governor);
rewards.updateTokenSpeed(depositToken, 0);

// 5. Residual balance is unclaimable:
//    - remaining users' accrued dust < gas cost, or exactly zero after truncation
//    - RewardsDistributor exposes no sweep/recover/rescue function
assertGt(rewardToken.balanceOf(address(rewards)), 0);   // locked forever
// No function on RewardsDistributor can move rewardToken other than
// _transferRewardIfEnoughTokens, reachable only via claimRewards for a
// nonzero accrued entitlement <= balance.
```