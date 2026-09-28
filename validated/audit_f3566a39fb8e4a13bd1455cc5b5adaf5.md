### Title
Stale `tokenSpeeds` causes incorrect reward emissions — `syncTokenSpeed` is never invoked from accrual paths - (File: contracts/RewardsDistributor.sol)

### Summary
`RewardsDistributor` accrues rewards per token using a stored `tokenSpeeds[token_]` value in `_calculateTokenIndex`. The analog to ChefIncentivesController's scheduled emission rate is the Vesper-derived speed, which is only refreshed by the keeper-only `syncTokenSpeed` function. None of the accrual entry points — `updateBeforeMintOrBurn`, `updateBeforeTransfer`, or `claimRewards` — resync the speed before computing `_tokensAccrued = _deltaTimestamps * _speed`, so the index accrues at a stale rate whenever the underlying Vesper `rewardRates`/`periodFinish` changes until the keeper happens to call `syncTokenSpeed`.

### Finding Description
In `contracts/RewardsDistributor.sol`:

- `_calculateTokenIndex` reads `tokenSpeeds[token_]` directly to compute `uint256 _tokensAccrued = _deltaTimestamps * _speed` (`lines 201-208`).
- The only function that recomputes the speed from the live Vesper pool is `syncTokenSpeed` (`lines 333-348`), which reads `IPoolRewardsExt.rewardRates` and `periodFinish` and enforces `_msgSender() != tokenSpeedKeeper → revert NotTokenSpeedKeeper`.
- `updateBeforeMintOrBurn` (`lines 175-180`), `updateBeforeTransfer` (`lines 186-192`), and `claimRewards` (`lines 150-168`) all call `_updateTokenIndex`/`_updateTokensAccruedOf` without any resync, identical to the Radiant pattern where `_updateEmissions` is missing from `_handleActionAfterForToken` and `afterLockUpdate`.

Two concrete staleness windows exist:

1. **Past `periodFinish`**: once `block.timestamp >= _rewards.periodFinish(rewardToken)`, the correct speed is `0`, but the stale nonzero speed keeps accruing. Every subsequent `updateBeforeMintOrBurn`/`updateBeforeTransfer`/`claimRewards` mints phantom rewards into `tokensAccruedOf`.
2. **Changed `rewardRates`**: when Vesper updates the reward rate (new `notifyRewardAmount`), accrual continues at the old speed — over-emission if the rate decreased, under-emission if it increased.

This is directly reachable by an unprivileged attacker: `updateBeforeMintOrBurn` is permissionless ("This function also may be called by anyone to update stored indexes") and is also invoked internally on every DepositToken mint/burn and DebtToken issue/repay, so the attacker controls when their `tokensAccruedOf` gets booked and can call `claimRewards(attacker)` to extract it.

### Impact Explanation
Theft of unclaimed yield / reward drainage. During the staleness window after `periodFinish` (or after a rate decrease), the index accrues rewards that should not exist. An attacker holding (or flash-minting via deposit) DepositToken/DebtToken balances books inflated `tokensAccruedOf` and drains the distributor's `rewardToken` balance via `claimRewards`, stealing rewards that belong to other users. Conversely, when the rate increases, legitimate users silently under-accrue until the keeper acts. `_transferRewardIfEnoughTokens` pays out whatever was accrued as long as the contract holds enough `rewardToken`, so the over-accrual converts directly to loss.

### Likelihood Explanation
High during every emission boundary. `syncTokenSpeed` requires a privileged keeper transaction; there is no automatic resync on accrual. On deployed chains (mainnet/optimism deployments present), each Vesper `periodFinish` rollover and each `notifyRewardAmount` rate change opens a window where accrual is wrong. The attacker needs only to hold or mint a deposit/debt token position and call public functions — no privileged role needed. `nonReentrant` on `claimRewards`, `onlyIfTokenExists`, and `onlyIfDistributorExists` do not block this path. The gas-costs-vs-accuracy tradeoff is the same one Radiant acknowledged; here the keeper-bottlenecked resync guarantees the window exists on every rate change.

### Recommendation
Resync the speed inside `_updateTokenIndex` (or at the top of `updateBeforeMintOrBurn`, `updateBeforeTransfer`, and `claimRewards`) before computing `_tokensAccrued`. Because `syncTokenSpeed`'s dependency data (`rewardRates`, `periodFinish`, `vPool.balanceOf(treasury)`) is externally readable, the resync can be performed permissionlessly — drop the `tokenSpeedKeeper` gate or add a `view`-safe internal `_syncSpeed(token_)` invoked on every accrual. Alternatively, at minimum, clamp accrual so no rewards accrue past `periodFinish` by storing the period end alongside `tokenSpeeds` and capping `_deltaTimestamps`.

### Proof of Concept
Hardhat fork sketch (mainnet fork, existing deployed `RewardsDistributor`):

```ts
// Setup: fork mainnet at a block just after Vesper poolRewards.periodFinish(rewardToken)
// for a depositToken whose underlying is a Vesper vPool.

const rd = await ethers.getContractAt("RewardsDistributor", RD_ADDR);
const depositToken = await ethers.getContractAt("DepositToken", DEPOSIT_TOKEN);
const vPool = await ethers.getContractAt("IVPool", await depositToken.underlying());
const rewards = await ethers.getContractAt("IPoolRewardsExt", await vPool.poolRewards());
const rewardToken = await rd.rewardToken();

assert((await rewards.periodFinish(rewardToken)).lt(await time.latest()));
// => correct speed is 0, but tokenSpeeds[depositToken] is still nonzero
assert((await rd.tokenSpeeds(depositToken.address)).gt(0));

// Attacker deposits underlying to mint depositToken (accruing position)
await depositToken.connect(attacker).deposit(amount);

// Advance time — index accrues at stale speed despite periodFinish having passed
await time.increase(86400);
await rd.updateBeforeMintOrBurn(depositToken.address, attacker.address); // permissionless

const accrued = await rd.tokensAccruedOf(attacker.address);
assert(accrued.gt(0)); // phantom rewards

await rd["claimRewards(address)"](attacker.address);
assert((await IERC20.at(rewardToken).balanceOf(attacker.address)).gt(0));
// rewardToken drained from distributor at expense of other users' unclaimed yield
```

The same test inverted (rate increase, attacker unaffected but honest users under-accrue) demonstrates the second direction. No keeper, governor, or privileged call is required by the attacker — the bug is reachable purely through public entry points.