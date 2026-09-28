### Title
Rewards accrue past Vesper `periodFinish` because `syncTokenSpeed` applies the stale rate until the next sync - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
`RewardsDistributor` accrues reward tokens lazily: `_calculateTokenIndex` multiplies the elapsed time since `tokenStates[token].timestamp` by the stored `tokenSpeeds[token]` ( [1](#0-0) ). The effective emission rate for Vesper-backed deposit tokens is synced from Vesper's `PoolRewards` via `syncTokenSpeed` ( [2](#0-1) ). When `block.timestamp >= periodFinish`, the synced speed becomes `0`, but `_updateTokenSpeed` first calls `_updateTokenIndex`, which credits rewards at the *old* speed for the entire interval `[lastTimestamp, now]` — including time after `periodFinish` ( [3](#0-2) ). This is the same bug class as the OTLM finding: rewards for a period are measured at the time the next period/update is triggered rather than being capped at the period's end, so more rewards are distributed than Vesper allocated.

### Finding Description
Vesper's `PoolRewards` defines a finite reward window `[start, periodFinish]` with a `rewardRate`. `syncTokenSpeed` mirrors that into Metronome by setting `speed = rewardRate * treasuryVPoolBalance / vPoolTotalSupply` while `block.timestamp < periodFinish`, and `0` afterwards ( [4](#0-3) ). However, the index is advanced with `block.timestamp - supplyState.timestamp` at whatever speed is currently stored — there is no `min(block.timestamp, periodFinish)` clamp. Between `periodFinish` and the moment the keeper calls `syncTokenSpeed`, holders continue accruing at the pre-expiry speed. The accrued delta materializes through `_updateTokensAccruedOf`, which any EOA can trigger via the permissionless `updateBeforeMintOrBurn`/`updateBeforeTransfer`/`claimRewards` entry points ( [5](#0-4) , [6](#0-5) ).

### Impact Explanation
Rewards exceeding Vesper's funded allocation are paid out of the distributor's `rewardToken` balance. `_transferRewardIfEnoughTokens` silently skips payment when `amount > balance` without clearing or honoring `tokensAccruedOf` ( [7](#0-6) ). Early claimers extract rewards that were never allocated for the post-`periodFinish` window, so later claimers' honestly accrued balances become permanently unpayable — theft of unclaimed yield. Unlike OTLM there is no strike-price loss, but the invariant "rewards distributed == rewards allocated for the period" breaks identically.

### Likelihood Explanation
The overrun magnitude equals `oldSpeed * (syncTime - periodFinish)`. It requires only that the keeper's sync (or a governor `updateTokenSpeed`) is not executed in the same block as `periodFinish`, which is almost always the case for time-based reward windows, and that the distributor holds enough `rewardToken` to pay the excess. No attacker action is needed to create the condition; any holder can harvest it with a single `claimRewards` call. The keeper is not malicious — the flaw is purely that the accrual math ignores `periodFinish`.

### Recommendation
Track the reward window end and clamp accrual: store `periodFinish` per token (or cap `_deltaTimestamps` at `periodFinish - timestamp`), so `_calculateTokenIndex` uses `min(block.timestamp, periodFinish)`. Alternatively, have `syncTokenSpeed` backdate the index timestamp to `periodFinish` when the window has already ended, mirroring the OTLM recommendation of recording an `epochRewardsPerTokenEnd` based on the period's actual end rather than the sync time.

### Proof of Concept
Hardhat fork sketch (Vesper-backed deposit token with active `tokenSpeed` synced from `PoolRewards`):

```ts
// 1. Keeper has synced; tokenSpeeds[depositToken] = S > 0, periodFinish = T.
// 2. Warp to T + 1 day (no one syncs; index timestamp still at last update t0 < T).
await time.increaseTo(periodFinish + ONE_DAY);
// 3. Keeper syncs: _updateTokenIndex accrues S * (now - t0) — includes (T - t0) + 1 day.
await rewardsDistributor.connect(keeper).syncTokenSpeed(depositToken.address);
// 4. Attacker claims: accrued index covers time past periodFinish.
await rewardsDistributor.claimRewards(attacker.address, [depositToken.address]);
// Assert: attacker reward > S * (T - t0) * balance / totalSupply, and distributor
// rewardToken balance is drained such that a second user's accrued rewards revert
// to unpaid (safeTransfer skipped by _transferRewardIfEnoughTokens).
```

Valid analog found; the finding above stands on `contracts/RewardsDistributor.sol`.

### Citations

**File:** contracts/RewardsDistributor.sol (L150-167)
```text
    function claimRewards(address[] memory accounts_, IERC20[] memory tokens_) public override nonReentrant {
        uint256 _accountsLength = accounts_.length;
        uint256 _tokensLength = tokens_.length;
        for (uint256 i; i < _tokensLength; ++i) {
            IERC20 _token = tokens_[i];

            if (tokenStates[_token].index > 0) {
                _updateTokenIndex(_token);
                for (uint256 j; j < _accountsLength; j++) {
                    _updateTokensAccruedOf(_token, accounts_[j]);
                }
            }
        }

        for (uint256 j; j < _accountsLength; j++) {
            address _account = accounts_[j];
            _transferRewardIfEnoughTokens(_account, tokensAccruedOf[_account]);
        }
```

**File:** contracts/RewardsDistributor.sol (L175-180)
```text
    function updateBeforeMintOrBurn(IERC20 token_, address account_) external override {
        if (tokenStates[token_].index > 0) {
            _updateTokenIndex(token_);
            _updateTokensAccruedOf(token_, account_);
        }
    }
```

**File:** contracts/RewardsDistributor.sol (L201-208)
```text
        uint256 _speed = tokenSpeeds[token_];
        uint256 _deltaTimestamps = block.timestamp - uint256(_supplyState.timestamp);
        if (_deltaTimestamps > 0 && _speed > 0) {
            uint256 _totalSupply = token_.totalSupply();
            uint256 _tokensAccrued = _deltaTimestamps * _speed;
            uint256 _ratio = _totalSupply > 0 ? _tokensAccrued.wadDiv(_totalSupply) : 0;
            _newIndex = (_supplyState.index + _ratio).toUint224();
            _newTimestamp = block.timestamp.toUint32();
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

**File:** contracts/RewardsDistributor.sol (L291-293)
```text
        uint256 _currentSpeed = tokenSpeeds[token_];
        if (_currentSpeed > 0) {
            _updateTokenIndex(token_);
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
