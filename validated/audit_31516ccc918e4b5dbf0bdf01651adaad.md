### Title
RewardsDistributor accrues rewards past Vesper `periodFinish` at stale speed, letting users drain other users' unclaimed rewards - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
`RewardsDistributor` mirrors a Vesper `PoolRewards` reward stream by caching a `tokenSpeeds[token]` rate that is only ever refreshed when the `tokenSpeedKeeper` calls `syncTokenSpeed()`. `syncTokenSpeed()` correctly zeroes the speed once `block.timestamp >= periodFinish`, but `_calculateTokenIndex()` has no `periodFinish` awareness: it accrues `deltaTimestamps * speed` from the last index timestamp all the way to `block.timestamp`. Between the moment `periodFinish` passes and the next keeper sync, the index keeps growing at the stale speed, crediting users with unbacked accrual. Claims are paid first-come-first-served from the distributor's finite `rewardToken` balance via `_transferRewardIfEnoughTokens`, so anyone who accrues and claims during this window steals rewards legitimately owed to other depositors/borrowers.

### Finding Description [1](#0-0) 

`_calculateTokenIndex` computes:

```solidity
uint256 _deltaTimestamps = block.timestamp - uint256(_supplyState.timestamp);
if (_deltaTimestamps > 0 && _speed > 0) {
    uint256 _tokensAccrued = _deltaTimestamps * _speed;
    ...
}
```

The only thing that bounds accrual is `tokenSpeeds[token_]`, and that is only reset inside `syncTokenSpeed`: [2](#0-1) 

```solidity
if (block.timestamp < _rewards.periodFinish(address(rewardToken))) {
    _speed = (rewardRates * balance) / totalSupply;
}
_updateTokenSpeed(IERC20(address(depositToken_)), _speed);
```

So for every timestamp `t` with `periodFinish <= t < nextSyncTime`, `_updateTokenIndex` accrues `_speed * (t - lastTimestamp)` including the entire `[lastTimestamp, t]` span beyond `periodFinish`. There is no clamp like `min(block.timestamp, periodFinish)` and no stored `periodFinish` in `TokenState`. Once the accrued amount is recorded in `tokensAccruedOf`, `claimRewards` pays it out as long as the distributor holds enough `rewardToken`: [3](#0-2) 

The accrual path is reachable by an unprivileged attacker: `updateBeforeMintOrBurn` is explicitly permissionless ("This function also may be called by anyone"), and `claimRewards` is public and not restricted by `SynthContext`, pause flags, or health checks. `_transferRewardIfEnoughTokens` pays out whoever accrued first, so the over-accrued delta is extracted from the same finite reward balance that honest users' legitimate accrual depends on.

### Impact Explanation
The reward-accual invariant "users can only claim `speed * min(now, periodFinish)` worth of emissions" is broken. Over-accrued index inflates `tokensAccruedOf` with unbacked rewards, and since the distributor's `rewardToken` balance is finite, earlier claimers receive tokens that economically belong to later claimers — theft of unclaimed yield from other users, directly analogous to the referenced finding's "users lose the final valid period" boundary mishandling, here expressed as failure to stop accrual at the boundary.

### Likelihood Explanation
Exploitation requires only a timing gap between `periodFinish` elapsing on the underlying Vesper `PoolRewards` and the next `syncTokenSpeed` keeper call — no privileged access, no flash manipulation, no oracle assumption. The longer the keeper's sync cadence, the larger the over-accrual. The attacker only needs to hold (or dust-mint) a tracked `DepositToken`/`DebtToken` balance and call `updateBeforeMintOrBurn` + `claimRewards` within the window. Since `MAX_TOKENS_PER_USER`-style gates don't apply, even a small balance accrues proportionally at the stale speed.

### Recommendation
Store the synced `periodFinish` per token (e.g., extend `TokenState` or a parallel mapping set in `syncTokenSpeed`) and cap accrual in `_calculateTokenIndex`:

```solidity
uint256 _end = periodFinishOf[token_];
uint256 _to = _end > 0 && _end < block.timestamp ? _end : block.timestamp;
uint256 _deltaTimestamps = _to > _supplyState.timestamp ? _to - _supplyState.timestamp : 0;
```

Alternatively, have `syncTokenSpeed` retroactively zero the effective speed at `periodFinish` by setting `tokenStates[token_].timestamp = periodFinish` when `_speed` becomes 0, so no accrual is credited past the end of the reward window.

### Proof of Concept
Foundry/Hardhat fork outline:

1. Fork a chain where a `RewardsDistributor` is registered on a `Pool` and `tokenSpeeds[depositToken] = s > 0` with `tokenStates[depositToken].timestamp = t0`, and `periodFinish = t0 + D` on the underlying `PoolRewards`.
2. Warp to `t0 + D + G` where `G > 0` and no `syncTokenSpeed` call has occurred (keeper gap).
3. Attacker (holding a `depositToken` balance, possibly dust) calls `rewardsDistributor.updateBeforeMintOrBurn(depositToken, attacker)` then `rewardsDistributor.claimRewards(attacker)`.
4. Observe `tokenStates.index` incremented by `s * (D + G) / totalSupply` instead of the correct `s * D / totalSupply`, and `tokensAccruedOf[attacker]` inflated by `s * G * balance / totalSupply`.
5. Compare `claimRewards` payout against the distributor's `rewardToken` balance and honest users' accrued amounts — the attacker's excess accrual is paid from balance that should cover honest users' claims, reducing what later claimers can withdraw (or leaving `tokensAccruedOf > balance` and silently underpaying via `_transferRewardIfEnoughTokens`).

Uncertainty noted: the exact deployed keeper cadence and whether a practical gap between `periodFinish` and `syncTokenSpeed` exists on live deployments was not verifiable from the indexed code alone; the contract-side defect (no accrual cap at `periodFinish`) is confirmed in `contracts/RewardsDistributor.sol`.

### Citations

**File:** contracts/RewardsDistributor.sol (L197-212)
```text
    function _calculateTokenIndex(
        TokenState memory _supplyState,
        IERC20 token_
    ) private view returns (uint224 _newIndex, uint32 _newTimestamp) {
        uint256 _speed = tokenSpeeds[token_];
        uint256 _deltaTimestamps = block.timestamp - uint256(_supplyState.timestamp);
        if (_deltaTimestamps > 0 && _speed > 0) {
            uint256 _totalSupply = token_.totalSupply();
            uint256 _tokensAccrued = _deltaTimestamps * _speed;
            uint256 _ratio = _totalSupply > 0 ? _tokensAccrued.wadDiv(_totalSupply) : 0;
            _newIndex = (_supplyState.index + _ratio).toUint224();
            _newTimestamp = block.timestamp.toUint32();
        } else if (_deltaTimestamps > 0 && _supplyState.index > 0) {
            _newTimestamp = block.timestamp.toUint32();
        }
    }
```

**File:** contracts/RewardsDistributor.sol (L248-256)
```text
    function _transferRewardIfEnoughTokens(address account_, uint256 amount_) private {
        IERC20 _rewardToken = rewardToken;
        uint256 _balance = _rewardToken.balanceOf(address(this));
        if (amount_ > 0 && amount_ <= _balance) {
            tokensAccruedOf[account_] = 0;
            _rewardToken.safeTransfer(account_, amount_);
            emit RewardClaimed(account_, amount_);
        }
    }
```

**File:** contracts/RewardsDistributor.sol (L333-348)
```text
    function syncTokenSpeed(IDepositToken depositToken_) external {
        if (_msgSender() != tokenSpeedKeeper) revert NotTokenSpeedKeeper();

        IVPool _vPool = IVPool(address(depositToken_.underlying()));
        IPoolRewardsExt _rewards = IPoolRewardsExt(_vPool.poolRewards());

        uint256 _speed;

        if (block.timestamp < _rewards.periodFinish(address(rewardToken))) {
            _speed =
                (_rewards.rewardRates(address(rewardToken)) * _vPool.balanceOf(address(pool.treasury()))) /
                _vPool.totalSupply();
        }

        _updateTokenSpeed(IERC20(address(depositToken_)), _speed);
    }
```
