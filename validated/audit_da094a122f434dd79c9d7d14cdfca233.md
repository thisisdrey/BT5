### Title
Zero `accountIndexOf` treated as "initialized from index 0" lets a pre-registered holder steal rewards equal to their full token balance - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
`RewardsDistributor._calculateTokenDelta` only applies the `INITIAL_INDEX` fallback when the global token index is strictly greater than `INITIAL_INDEX`. When a reward token is registered, its index starts at exactly `INITIAL_INDEX` (1e18). Any account that already holds a `DepositToken`/`DebtToken` balance but has `accountIndexOf == 0` (i.e., deposited/borrowed before rewards were configured for that token) is treated as if its account index were `0`, producing a delta of `1e18` and an accrued reward equal to its entire token balance. This is a lifecycle/initialization-order analog of the CVE's object-lifecycle heap corruption: an object (account reward state) is used in a half-initialized state.

### Finding Description
In `_calculateTokenDelta`:

```solidity
if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}
uint256 _deltaIndex = _tokenIndex - _accountIndex;
_tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
``` [1](#0-0) 

When `_updateTokenSpeed` first registers a token, it stores `TokenState({index: INITIAL_INDEX, ...})`. [2](#0-1)  `_updateTokenIndex` only advances the index when `block.timestamp - timestamp > 0` and `speed > 0`. [3](#0-2)  Therefore, within the same block/timestamp in which the token is registered, `tokenStates[token_].index == INITIAL_INDEX`, the fallback's `_tokenIndex > INITIAL_INDEX` condition is false, and `_accountIndex` remains `0`. `_deltaIndex` becomes `1e18` and `_tokensDelta = balanceOf(account_).wadMul(1e18) = balanceOf(account_)`.

`claimRewards` then calls `_transferRewardIfEnoughTokens`, which pays out `tokensAccruedOf[account_]` from the distributor's reward-token balance with no cross-check. [4](#0-3) [5](#0-4) 

### Impact Explanation
An attacker who holds (or flash-acquires in the same transaction, via `DepositToken._mint`/`DebtToken` issue — which set `accountIndexOf` only when the token's index is already > 0; before registration the hook at `updateBeforeMintOrBurn` is a no-op because `index == 0`) a large `DepositToken`/`DebtToken` balance can call `claimRewards` in the same block that the governor registers the token's speed, receiving reward tokens equal to their full token balance (up to the distributor's entire reward-token balance). This is direct theft of unclaimed yield belonging to all other depositors/borrowers. The exploit is repeatable across every newly registered reward token and across chains.

### Likelihood Explanation
- `updateBeforeMintOrBurn`/`updateBeforeTransfer`/`claimRewards` are all permissionless and unauthenticated. [6](#0-5) 
- No modifier blocks the edge case: `nonReentrant` does not help, and `onlyIfTokenExists` is only checked on the governor's registration call, not on the claim path. [7](#0-6) 
- The window is the same block as the `updateTokenSpeed` registration transaction (and, more generally, any state where `index == INITIAL_INDEX` exactly, e.g., a token registered but whose index has not yet advanced). An attacker can pre-position a balance before rewards are enabled and back-run the governor's registration transaction in the same block — a standard MEV pattern, fully within unprivileged attacker capabilities.
- Scaling the position is cheap: the required balance can be obtained via depositing collateral (any size up to `maxTotalSupply`) or issuing debt, and can be leveraged with flash-borrowed capital since only the balance snapshot at claim time matters.

### Recommendation
Change the fallback to trigger whenever the stored account index is `0` and the token index is initialized (>= `INITIAL_INDEX`), i.e.:

```solidity
if (_accountIndex == 0 && _tokenIndex >= INITIAL_INDEX) {
    _accountIndex = _tokenIndex;
}
```

Setting `_accountIndex = _tokenIndex` (not `INITIAL_INDEX`) also removes the stale-index edge case where a pre-existing holder would otherwise be credited for the full `[INITIAL_INDEX, current]` range on their first interaction — the first-touch index should always be the current index. Alternatively, set `accountIndexOf` inside the `updateRewardsBeforeMintOrBurn`/`updateRewardsBeforeTransfer` modifiers even when `index == 0`.

### Proof of Concept
Foundry fork test sketch against a deployed `RewardsDistributor` + `Pool` (e.g., mainnet deployments in `deployments/mainnet/`):

```solidity
function test_stealRewardsOnRegistration() public {
    // 1. Before rewards are configured for msdX, attacker deposits collateral.
    //    updateBeforeMintOrBurn is a no-op because tokenStates[msdX].index == 0.
    vm.prank(attacker);
    pool.deposit(address(depositTokenX), address(underlying), depositAmount, ...);
    assertEq(distributor.accountIndexOf(msdX, attacker), 0);

    // 2. Governor registers the token; attacker back-runs in the same block/timestamp.
    vm.prank(governor);
    distributor.updateTokenSpeed(msdX, speed);
    // same block: tokenStates[msdX].index == INITIAL_INDEX (deltaTimestamps == 0)

    // 3. claimRewards computes delta = INITIAL_INDEX - 0 = 1e18
    //    => tokensDelta = balanceOf(attacker)  => drains rewardToken up to distributor balance
    uint256 balBefore = rewardToken.balanceOf(attacker);
    vm.prank(attacker);
    distributor.claimRewards(attacker);
    assertEq(rewardToken.balanceOf(attacker) - balBefore,
             min(depositTokenX.balanceOf(attacker), rewardToken.balanceOf(address(distributor))));
}
```

Note: same-block ordering can be achieved via a bundle/back-run; alternatively, any path where `updateBeforeMintOrBurn` runs while `tokenStates[token_].index == INITIAL_INDEX` and `accountIndexOf == 0` (e.g., a first deposit in the registration block) produces the identical corrupted delta.

### Citations

**File:** contracts/RewardsDistributor.sol (L91-97)
```text
    modifier onlyIfTokenExists(address token_) {
        IPool _pool = pool;
        if (!_pool.doesDebtTokenExist(IDebtToken(token_)) && !_pool.doesDepositTokenExist(IDepositToken(token_))) {
            revert InvalidToken();
        }
        _;
    }
```

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

**File:** contracts/RewardsDistributor.sol (L175-192)
```text
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

**File:** contracts/RewardsDistributor.sol (L221-231)
```text
    ) private view returns (uint256 _tokenIndex, uint256 _tokensDelta) {
        _tokenIndex = _tokenState.index;
        uint256 _accountIndex = accountIndexOf[token_][account_];

        if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
            _accountIndex = INITIAL_INDEX;
        }

        uint256 _deltaIndex = _tokenIndex - _accountIndex;
        _tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
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

**File:** contracts/RewardsDistributor.sol (L294-299)
```text
        } else if (newSpeed_ > 0) {
            // Add token to the list
            if (tokenStates[token_].index == 0) {
                if (tokens.length == MAX_REWARD_TOKENS) revert ReachedMaxRewardTokens();
                tokenStates[token_] = TokenState({index: INITIAL_INDEX, timestamp: block.timestamp.toUint32()});
                tokens.push(token_);
```
