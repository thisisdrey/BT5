### Title
Rewards emitted while `totalSupply == 0` are permanently stuck in `RewardsDistributor` - (File: contracts/RewardsDistributor.sol)

### Summary
`RewardsDistributor` distributes `rewardToken` linearly at `tokenSpeeds[token_]` per second via a supply index. In `_calculateTokenIndex`, when the token's `totalSupply` is 0 the accrued ratio is set to 0 but the timestamp is still advanced, so the rewards attributable to that period are never allocated to anyone and remain permanently locked in the contract, which has no recovery/sweep function for `rewardToken`.

### Finding Description
In `contracts/RewardsDistributor.sol`, `_calculateTokenIndex` computes `_ratio = _totalSupply > 0 ? _tokensAccrued.wadDiv(_totalSupply) : 0` and unconditionally bumps `_newTimestamp` to `block.timestamp`. [1](#0-0)  The `_tokensAccrued = _deltaTimestamps * _speed` emitted during any window where the DepositToken/DebtToken supply is zero are simply dropped from the index rather than carried forward. [2](#0-1)  The timestamp advance is persisted by `_updateTokenIndex`, which is invoked from the public, permissionless `updateBeforeMintOrBurn`, `updateBeforeTransfer`, and `claimRewards` entry points. [3](#0-2)  `claimRewards` only pays out up to `tokensAccruedOf[account_]`, and `_transferRewardIfEnoughTokens` never sends the leftover balance to anyone; the contract exposes no function to withdraw `rewardToken`. [4](#0-3) 

This is the same class as the Truflation `VirtualStakingRewards` report: a fixed emission rate (`tokenSpeed` ≈ `rewardRate`) is assumed to be fully distributed, but periods with zero supply silently consume emission time without crediting it.

### Impact Explanation
Permanent freezing of unclaimed yield. Any reward tokens streamed while a tracked token's supply is zero become unreachable: the index never reflects them, `claimRewards` cannot pay them out, and there is no rescue function, so they sit in the distributor forever. The magnitude equals `speed * (duration of zero-supply periods)`.

### Likelihood Explanation
- `syncTokenSpeed` (permissioned to `tokenSpeedKeeper`, but the loss condition itself is permissionless) and governor speed updates can be applied while a DepositToken has zero supply — e.g., a newly onboarded deposit token before first deposit. [5](#0-4) 
- An unprivileged user can create the zero-supply window: burn all `DepositToken`/`DebtToken` supply (withdraw/repay everything or burn via transfers), let time pass with `speed > 0`, then call `updateBeforeMintOrBurn` (callable by anyone) to lock in the timestamp advance and lose the accrued rewards. [6](#0-5) 
- No supply cap, pause flag, reentrancy guard, or SynthContext check prevents the zero-supply window or the timestamp update. The only gates are `onlyGovernor`/`tokenSpeedKeeper` on setting speeds, which does not prevent the loss once speed is nonzero.

### Recommendation
Track undistributed rewards explicitly. E.g., accumulate `undistributed += _deltaTimestamps * _speed` whenever `_totalSupply == 0`, and fold it back into the index (or into an effective speed boost) when supply returns; alternatively keep the timestamp frozen while supply is zero so emission time is not consumed. Also consider a governor-only `recoverRewardToken` limited to the provably-undistributed amount.

### Proof of Concept
Hardhat (based on `test/RewardDistributor.test.ts` harness which uses a mocked `msdTOKEN1`):

```ts
it('loses rewards emitted while totalSupply == 0', async function () {
    const speed = parseEther('1') // 1 rewardToken / second
    await rewardDistributor.updateTokenSpeed(msdTOKEN1.address, speed)

    // zero supply period: 10 seconds elapse with totalSupply == 0
    msdTOKEN1.totalSupply.returns(0)
    await increaseTimeOfNextBlock(10)
    await rewardDistributor.updateBeforeMintOrBurn(msdTOKEN1.address, alice.address)

    // index did NOT accrue the 10 tokens, but timestamp advanced
    const {index, timestamp} = await rewardDistributor.tokenStates(msdTOKEN1.address)
    expect(index).eq(DEFAULT_INDEX) // no accrual recorded

    // bob deposits; now sole holder of 100 totalSupply for 10 more seconds
    msdTOKEN1.totalSupply.returns(parseEther('100'))
    msdTOKEN1.balanceOf.returns(parseEther('100'))
    await increaseTimeOfNextBlock(10)
    await rewardDistributor.updateBeforeMintOrBurn(msdTOKEN1.address, bob.address)

    // bob can only ever claim the last 10s of emissions; the first 10 rewards
    // are neither in the index nor claimable by anyone, and there is no
    // function to withdraw the stranded rewardToken balance.
    const bobAccrued = await rewardDistributor.tokensAccruedOf(bob.address)
    expect(bobAccrued).eq(parseEther('10')) // not 20
})
```

On a fork this is reproducible by fully withdrawing a rewards-enabled deposit token, warping time, calling `updateBeforeMintOrBurn` permissionlessly, redepositing, and observing the shortfall between `rewardToken.balanceOf(distributor)` growth and total claimable.

### Citations

**File:** contracts/RewardsDistributor.sol (L175-180)
```text
    function updateBeforeMintOrBurn(IERC20 token_, address account_) external override {
        if (tokenStates[token_].index > 0) {
            _updateTokenIndex(token_);
            _updateTokensAccruedOf(token_, account_);
        }
    }
```

**File:** contracts/RewardsDistributor.sol (L202-211)
```text
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

**File:** contracts/RewardsDistributor.sol (L271-282)
```text
    function _updateTokenIndex(IERC20 token_) private {
        TokenState storage _supplyState = tokenStates[token_];
        (uint224 _newIndex, uint32 _newTimestamp) = _calculateTokenIndex(_supplyState, token_);
        if (_newIndex > 0 && _newTimestamp > 0) {
            _supplyState.index = _newIndex;
            _supplyState.timestamp = _newTimestamp;
            emit TokenIndexUpdated(_newIndex, _newTimestamp);
        } else if (_newTimestamp > 0) {
            _supplyState.timestamp = _newTimestamp;
            emit TokenIndexUpdated(_supplyState.index, _newTimestamp);
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
