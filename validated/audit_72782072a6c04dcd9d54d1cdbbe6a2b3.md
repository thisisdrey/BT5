### Title
Re-enabling a disabled reward token (speed 0 → non-zero) lets late depositors drain the whole historical index, stranding honest users' rewards - (File: contracts/RewardsDistributor.sol)

### Summary
`RewardsDistributor` has no "remove token" path per se, but `updateTokenSpeed(token, 0)` (and the keeper-driven `syncTokenSpeed`, which naturally sets `_speed = 0` once `periodFinish` passes) is the functional equivalent of removing a reward token: accrual stops while the token stays in `tokens[]` and its `tokenStates[token].index` is frozen at its last value. When the same token is later re-enabled (`_updateTokenSpeed` sets `newSpeed_ > 0` without resetting the index), any account that entered while speed was 0 has `accountIndexOf[token][account] == 0`, and the `INITIAL_INDEX` fallback in `_calculateTokenDelta` credits it `balance * (currentIndex - INITIAL_INDEX)` — i.e., the entire reward history since the distributor started, not just since the user deposited. This is the same bug class as the report: a stale, never-initialized "paid" marker (`userRewardPerTokenPaid` / `accountIndexOf`) combined with a non-reset global accumulator (`rewardPerTokenStored` / `index`) after a token is removed and re-added.

### Finding Description
1. `onlyGovernor` `updateTokenSpeed` (or the permissionless-in-effect keeper call `syncTokenSpeed`, which computes `_speed = 0` after `periodFinish`) sets `tokenSpeeds[token] = 0`. From then on `_calculateTokenIndex` takes the `_deltaTimestamps > 0 && index > 0` branch and only bumps `timestamp`, freezing `index`. [1](#0-0) 
2. New users deposit / receive `DepositToken`/`DebtToken` while speed is 0. `updateBeforeMintOrBurn`/`updateBeforeTransfer` do run, but since `accountIndexOf[token][user]` was never written, it stays `0`. [2](#0-1) 
3. Governor re-enables emissions (or the keeper calls `syncTokenSpeed` once Vesper `notifyRewardAmount` rolls a new period). `_updateTokenSpeed` re-arms the token: `tokenStates[token].index` keeps its large historical value — it is never reset — and accrual resumes from it. [3](#0-2) 
4. On the late depositor's next touch, `_calculateTokenDelta` sees `_accountIndex == 0 && _tokenIndex > INITIAL_INDEX` and substitutes `INITIAL_INDEX`, so `_tokensDelta = balance * (index_now − INITIAL_INDEX)` — rewards for the full pre-removal history plus the new accrual, all at once. [4](#0-3) 
5. `updateBeforeMintOrBurn` is explicitly callable by anyone, so the attacker can force the accrual and then call `claimRewards`. [5](#0-4) 

### Impact Explanation
The distributor pays out the same historical index range twice: once (implicitly, via deltas) to early users and again to every account whose `accountIndexOf` is still 0 when the token is re-enabled. Early claimers drain `rewardToken` held by the distributor, and `_transferRewardIfEnoughTokens` silently skips transfers when the balance is insufficient — honest users' accrued `tokensAccruedOf` become permanently unclaimable while remaining non-zero. [6](#0-5)  This is theft of unclaimed yield plus permanent freezing of other users' yield, matching the report's "excess rewards / stranded rewards / claimRewards failure" impact.

### Likelihood Explanation
- The re-enable trigger does not require a malicious governor: `syncTokenSpeed` is designed to be called routinely by `tokenSpeedKeeper`, and it legitimately sets speed to 0 between Vesper reward periods and back to non-zero on each new `notifyRewardAmount` — i.e., the remove/re-add cycle is part of normal operation. [7](#0-6) 
- The attacker needs no privileges: hold (or flash-acquire) `DepositToken`/`DebtToken` while speed is 0, wait for re-enable, then call the permissionless `updateBeforeMintOrBurn` and `claimRewards`. No pause, lock, or health check intervenes, and `claimRewards`'s `nonReentrant` guard does not prevent the accounting discrepancy.
- The same fallback also lets an attacker who acquired tokens via `transfer` during the dead period claim retroactively, so multiple attackers can front-run each other to drain the balance first.

### Recommendation
When `accountIndexOf[token][account] == 0` and the token's index has already advanced past `INITIAL_INDEX`, initialize the account's baseline to the *current* `index` (not `INITIAL_INDEX`) unless the account provably held a positive balance during that period — i.e., first-touch should set `accountIndexOf[token][account] = tokenStates[token].index` rather than crediting `balance * (index - INITIAL_INDEX)`. Alternatively, record a per-token "seen" flag so the `INITIAL_INDEX` shortcut only applies to balances that existed before the token was first enabled, and reset/epoch the index when a token is re-enabled after a speed-0 period. The `_transferRewardIfEnoughTokens` silent-skip should also be revisited so drained-state users are distinguishable from users with zero accrual.

### Proof of Concept
Hardhat sketch (mirroring `test/RewardDistributor.test.ts` fixtures):

```ts
// 1. Speed active: alice deposits/accumulates; index grows to I1 > INITIAL_INDEX
await rewardDistributor.updateTokenSpeed(msdTOKEN1.address, speed)   // governor, or via syncTokenSpeed keeper
// time passes; alice updates
await rewardDistributor.updateBeforeMintOrBurn(msdTOKEN1.address, alice.address)
// alice.accountIndexOf = I1

// 2. Speed dropped to 0 (Vesper period ends -> keeper sync, or governor sets 0)
await rewardDistributor.connect(keeper).syncTokenSpeed(msdTOKEN1.address) // _speed = 0
// index frozen at I1

// 3. Attacker (bob) deposits/transfers-in while speed == 0
//    bob.accountIndexOf[msdTOKEN1] stays 0
msdTOKEN1.balanceOf.whenCalledWith(bob.address).returns(parseEther('1000'))
await rewardDistributor.updateBeforeMintOrBurn(msdTOKEN1.address, bob.address)

// 4. New Vesper period starts -> keeper re-enables speed (>0); index resumes from I1
await rewardDistributor.connect(keeper).syncTokenSpeed(msdTOKEN1.address)

// 5. Bob claims: _calculateTokenDelta gives bob balance * (I1+delta - INITIAL_INDEX)
await rewardDistributor.updateBeforeMintOrBurn(msdTOKEN1.address, bob.address)
const bobAccrued = await rewardDistributor.tokensAccruedOf(bob.address)
// bobAccrued >> bob's fair share; it includes the whole pre-removal epoch
await rewardDistributor['claimRewards(address)'](bob.address)

// 6. Alice's claimable > distributor balance -> _transferRewardIfEnoughTokens skips
//    alice.tokensAccruedOf stays > 0 forever -> stranded rewards
```

Expected: `bobAccrued` equals ~`1000 * (index_now - INITIAL_INDEX)` — rewards Bob did not earn — and `vsp.balanceOf(rewardDistributor)` is drained so Alice's `claimRewards` transfers nothing while `tokensAccruedOf[alice]` remains positive.

### Citations

**File:** contracts/RewardsDistributor.sol (L171-192)
```text
     * @notice Update indexes on pre-mint and pre-burn
     * @dev Called by DepositToken and DebtToken contracts
     * This function also may be called by anyone to update stored indexes
     */
    function updateBeforeMintOrBurn(IERC20 token_, address account_) external override {
        if (tokenStates[token_].index > 0) {
            _updateTokenIndex(token_);
            _updateTokensAccruedOf(token_, account_);
        }
    }

    /**
     * @notice Update indexes on pre-transfer
     * @dev Called by DepositToken and DebtToken contracts
     */
    function updateBeforeTransfer(IERC20 token_, address from_, address to_) external override {
        if (tokenStates[token_].index > 0) {
            _updateTokenIndex(token_);
            _updateTokensAccruedOf(token_, from_);
            _updateTokensAccruedOf(token_, to_);
        }
    }
```

**File:** contracts/RewardsDistributor.sol (L203-211)
```text
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

**File:** contracts/RewardsDistributor.sol (L221-230)
```text
    ) private view returns (uint256 _tokenIndex, uint256 _tokensDelta) {
        _tokenIndex = _tokenState.index;
        uint256 _accountIndex = accountIndexOf[token_][account_];

        if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
            _accountIndex = INITIAL_INDEX;
        }

        uint256 _deltaIndex = _tokenIndex - _accountIndex;
        _tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```

**File:** contracts/RewardsDistributor.sol (L248-255)
```text
    function _transferRewardIfEnoughTokens(address account_, uint256 amount_) private {
        IERC20 _rewardToken = rewardToken;
        uint256 _balance = _rewardToken.balanceOf(address(this));
        if (amount_ > 0 && amount_ <= _balance) {
            tokensAccruedOf[account_] = 0;
            _rewardToken.safeTransfer(account_, amount_);
            emit RewardClaimed(account_, amount_);
        }
```

**File:** contracts/RewardsDistributor.sol (L287-309)
```text
    function _updateTokenSpeed(
        IERC20 token_,
        uint256 newSpeed_
    ) private onlyIfDistributorExists onlyIfTokenExists(address(token_)) {
        uint256 _currentSpeed = tokenSpeeds[token_];
        if (_currentSpeed > 0) {
            _updateTokenIndex(token_);
        } else if (newSpeed_ > 0) {
            // Add token to the list
            if (tokenStates[token_].index == 0) {
                if (tokens.length == MAX_REWARD_TOKENS) revert ReachedMaxRewardTokens();
                tokenStates[token_] = TokenState({index: INITIAL_INDEX, timestamp: block.timestamp.toUint32()});
                tokens.push(token_);
            } else {
                // Update timestamp to ensure extra interest is not accrued during the prior period
                tokenStates[token_].timestamp = block.timestamp.toUint32();
            }
        }

        if (_currentSpeed != newSpeed_) {
            tokenSpeeds[token_] = newSpeed_;
            emit TokenSpeedUpdated(token_, _currentSpeed, newSpeed_);
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
