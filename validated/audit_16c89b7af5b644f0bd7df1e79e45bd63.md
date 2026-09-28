### Title
RewardsDistributor pays out reward tokens equal to a user's full token balance when claimed in the same block a reward token is registered - (File: contracts/RewardsDistributor.sol)

### Summary
When a governor registers a new reward token via `updateTokenSpeed`/`updateTokenSpeeds`, the token's index is initialized to `INITIAL_INDEX` (1e18). In `_calculateTokenDelta`, the fallback that substitutes `INITIAL_INDEX` for a missing `accountIndexOf` entry only applies when the token index is *greater than* `INITIAL_INDEX`. If `claimRewards` is called in the same block the token was added (index still exactly `INITIAL_INDEX`), an account with no stored index gets `_deltaIndex = 1e18`, i.e. `_tokensDelta = balance * 1e18 / 1e18 = balance`. An attacker can therefore claim reward tokens equal to their entire DepositToken/DebtToken balance, inflated arbitrarily with a flash loan.

### Finding Description
In `_updateTokenSpeed`, when `tokenStates[token_].index == 0` the state is set to `TokenState({index: INITIAL_INDEX, timestamp: block.timestamp})` [1](#0-0) . In `_calculateTokenDelta`, the "missing account index" allocation step only fires when `_tokenIndex > INITIAL_INDEX` [2](#0-1) . During the same block as registration, `_calculateTokenIndex` returns early because `_deltaTimestamps == 0` (or speed was just set), so `_updateTokenIndex` keeps `index == INITIAL_INDEX` [3](#0-2) . `claimRewards` is permissionless and calls `_updateTokensAccruedOf` for any token whose index > 0 — which includes the freshly registered token [4](#0-3) . `_transferRewardIfEnoughTokens` then pays out `tokensAccruedOf[account]` from the distributor's balance [5](#0-4) . This is the analog of the missing-allocation crash: the account-index allocation step is skipped for the boundary case `index == INITIAL_INDEX`.

### Impact Explanation
Attacker flash-borrows the underlying, deposits into the pool to mint a huge `DepositToken` balance, calls `claimRewards(attacker)` in the same block the governor's `updateTokenSpeed` transaction executes, then withdraws and repays the flash loan. They receive `rewardToken` equal to their inflated deposit-token balance, draining the RewardsDistributor and stealing yield belonging to legitimate users. This is direct theft of unclaimed yield / reward funds.

### Likelihood Explanation
Requires a governor transaction that registers a reward token (a normal, recurring operation when reward programs start), and the attacker must land `claimRewards` in the same block — achievable by monitoring the mempool and sandwiching/backrunning the governor tx. No privileged role is needed for the attack itself; flash loans make the required balance arbitrarily large, and the reward payout is capped only by the distributor's balance.

### Recommendation
Change the fallback condition to `>=` (`_tokenIndex >= INITIAL_INDEX`) or, more robustly, treat the zero account index as `INITIAL_INDEX` unconditionally when `accountIndexOf[token_][account_] == 0` and the token state exists — i.e., always perform the missing allocation step. Alternatively, record `accountIndexOf` lazily as `INITIAL_INDEX` in `updateBeforeMintOrBurn`/`updateBeforeTransfer` for accounts whose index is 0.

### Proof of Concept
Hardhat/foundry sketch (fork or local deployment):

```solidity
// assume pool, depositToken (mUSD deposit token), rewardsDistributor, rewardToken
// governor tx pending: rewardsDistributor.updateTokenSpeed(depositToken, speed)

// attacker contract, same block as governor tx (sandwich):
uint256 flashAmt = 1_000_000e18;
// 1. flash borrow `underlying`
// 2. pool.deposit(depositToken, flashAmt)  -> attacker gets depositToken balance ~flashAmt
// 3. rewardsDistributor.claimRewards(attacker)
//    - tokenStates[depositToken].index == INITIAL_INDEX (1e18)
//    - accountIndexOf[depositToken][attacker] == 0
//    - _tokenIndex > INITIAL_INDEX is false -> _accountIndex stays 0
//    - _tokensDelta = flashAmt.wadMul(1e18) = flashAmt
//    - distributor transfers min(flashAmt, rewardToken.balance) to attacker
// 4. pool.withdraw(depositToken, ...) -> repay flash loan
assertEq(rewardToken.balanceOf(attacker), expectedStolen);
```

Precondition for the PoC: call `claimRewards` in the same `block.timestamp` in which `updateTokenSpeeds` registered the token (in tests, set `vm.warp`/`evm` so both txs share a timestamp, or simply don't advance time after the governor call).

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

**File:** contracts/RewardsDistributor.sol (L201-211)
```text
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
```

**File:** contracts/RewardsDistributor.sol (L225-230)
```text
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

**File:** contracts/RewardsDistributor.sol (L296-299)
```text
            if (tokenStates[token_].index == 0) {
                if (tokens.length == MAX_REWARD_TOKENS) revert ReachedMaxRewardTokens();
                tokenStates[token_] = TokenState({index: INITIAL_INDEX, timestamp: block.timestamp.toUint32()});
                tokens.push(token_);
```
