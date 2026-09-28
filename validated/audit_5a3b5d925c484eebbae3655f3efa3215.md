### Title
Zero-initialized `accountIndexOf` poisons `tokensAccruedOf` for pre-existing holders when a reward token's index is still `INITIAL_INDEX`, permanently freezing their claimable yield - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary

`RewardsDistributor._calculateTokenDelta()` contains a fallback intended to handle accounts that have never had their reward index written (`accountIndexOf[token][account] == 0`). The fallback only applies when `_tokenIndex > INITIAL_INDEX`:

```solidity
uint256 _accountIndex = accountIndexOf[token_][account_];

if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}

uint256 _deltaIndex = _tokenIndex - _accountIndex;
_tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
``` [1](#0-0) 

When a token's stored index is exactly `INITIAL_INDEX` (1e18) — i.e., the token was registered via `_updateTokenSpeed()` but no index accrual has occurred yet — the fallback is skipped, `_accountIndex` remains `0`, and `_deltaIndex = 1e18`. For any account holding a non-zero balance, `_tokensDelta = balance * 1e18` is added to `tokensAccruedOf[account]`, permanently inflating it far beyond the distributor's reward token balance. Since `_transferRewardIfEnoughTokens()` only pays out when `amount_ <= balance`, the account's rewards become permanently unclaimable.

This is the direct analog of the reported bug: state (`accountIndexOf`) that was never initialized/migrated for pre-existing holders produces a wildly inflated accrual delta instead of being baselined to `INITIAL_INDEX`.

### Finding Description

When `_updateTokenSpeed()` registers a new reward token, it sets `tokenStates[token_] = TokenState({index: INITIAL_INDEX, timestamp: block.timestamp})` — the index starts at exactly `1e18`. [2](#0-1) 

From that point, three conditions hold simultaneously:

1. `claimRewards()` and `updateBeforeMintOrBurn()`/`updateBeforeTransfer()` process the token because `tokenStates[token_].index > 0`. [3](#0-2) 
2. `_updateTokenIndex()` cannot push the index above `INITIAL_INDEX` while `block.timestamp == tokenState.timestamp` or `tokenSpeeds[token_] == 0`. [4](#0-3) 
3. Any account that already held a `DepositToken`/`DebtToken` balance before registration still has `accountIndexOf[token][account] == 0`.

For such an account, `_calculateTokenDelta` computes `_deltaIndex = INITIAL_INDEX - 0 = 1e18` and `_tokensDelta = balance.wadMul(1e18) = balance * 1e18`. `_updateTokensAccruedOf` then stores `tokensAccruedOf[account] += balance * 1e18` and sets `accountIndexOf` to the current index, so the poisoning is one-shot and irreversible for that account. [5](#0-4) 

Crucially, `claimRewards(address[] accounts_, IERC20[] tokens_)` is permissionless and iterates over arbitrary `accounts_`. An unprivileged attacker can therefore call `claimRewards([victim], [depositToken])` during the window and poison the victim's `tokensAccruedOf` without the victim taking any action. [6](#0-5) 

Afterward, `_transferRewardIfEnoughTokens` checks `amount_ <= _balance`; since `tokensAccruedOf[victim]` is `balance * 1e18`, vastly larger than the distributor's reward token balance, the transfer branch is never reached, and `tokensAccruedOf[victim]` is never reset. [7](#0-6) 

The reachable windows are:

- The block (and subsequent blocks while `speed == 0`) in which a governor or `tokenSpeedKeeper` registers a token via `updateTokenSpeed()`/`updateTokenSpeeds()`/`syncTokenSpeed()`. `syncTokenSpeed` explicitly sets `_speed = 0` when the Vesper `periodFinish` has passed, which holds the index at `INITIAL_INDEX` indefinitely. [8](#0-7) 
- Any period where a registered token's speed is `0` (index frozen at `INITIAL_INDEX`) while holders exist who have never had an index update for that token.

No modifier stops this: `claimRewards` is `nonReentrant` but open to any caller for any account, and `onlyIfTokenExists` is not applied to `tokens_` in `claimRewards` (the `index > 0` check is what gates execution, and it passes).

### Impact Explanation

Permanent freezing of unclaimed yield. Once `tokensAccruedOf[victim]` is inflated to `balance * 1e18`, the victim can never claim rewards from this distributor again: `_transferRewardIfEnoughTokens` requires `amount_ <= balanceOf(distributor)`, a condition that cannot be met. All legitimately accrued rewards for that account — including rewards earned before and after the poisoning — are locked forever. The attacker can apply this to every pre-existing holder of a newly registered reward token in a single transaction via `claimRewards(accounts_, tokens_)`, mass-freezing yield for the whole holder set of that token.

### Likelihood Explanation

The trigger does not require malicious governance — it requires only the normal operational event of registering a reward token (or a `syncTokenSpeed` call that resolves to `_speed = 0` after `periodFinish`, which the code explicitly supports). The window persists as long as `tokenStates[token].index == INITIAL_INDEX`, which is indefinite whenever speed is 0. Any user can trigger it against any victim. The constraint is that the distributor must still hold reward tokens and victims must have held a balance before registration — both common in the deployed configuration.

### Recommendation

In `_calculateTokenDelta`, treat `accountIndexOf == 0` as `INITIAL_INDEX` unconditionally (or whenever `accountIndexOf == 0 && _tokenIndex >= INITIAL_INDEX`), not only when `_tokenIndex > INITIAL_INDEX`:

```solidity
if (_accountIndex == 0) {
    _accountIndex = INITIAL_INDEX;
}
```

This baselines every uninitialized account at the token's starting index regardless of whether accrual has occurred yet, eliminating the inflated `_deltaIndex`. Alternatively, write `accountIndexOf` lazily at balance-acquisition time only, and skip accrual entirely when `_accountIndex == 0`.

### Proof of Concept

Foundry/Hardhat fork sketch against a deployed pool + `RewardsDistributor`:

```solidity
// Setup: victim holds depositToken balance BEFORE reward token registration
depositToken.deposit(amount, victim);          // victim balance > 0, accountIndexOf == 0

// Governor (or tokenSpeedKeeper via syncTokenSpeed) registers the token.
// If speed == 0 (or this tx is in the same block), index stays == INITIAL_INDEX.
rewardsDistributor.updateTokenSpeed(depositToken, 0); // or syncTokenSpeed after periodFinish

// Attacker: unprivileged, poisons victim
address[] memory accts = new address[](1); accts[0] = victim;
IERC20[] memory toks = new IERC20[](1); toks[0] = IERC20(address(depositToken));
rewardsDistributor.claimRewards(accts, toks);

// Assert: tokensAccruedOf[victim] == victimBalance * 1e18
// Assert: victim can never claim — amount > distributor's rewardToken balance
assertGt(rewardsDistributor.claimable(victim), rewardToken.balanceOf(address(rewardsDistributor)));
// Any subsequent claimRewards(victim) transfers 0 and never resets tokensAccruedOf
```

Equivalent paths that poison without an attacker: the victim's own next `deposit`, `withdraw`, `transfer`, `issue`, `repay`, or `seize` on the affected token calls `updateBeforeMintOrBurn`/`updateBeforeTransfer`, which runs `_updateTokensAccruedOf` with the same `accountIndexOf == 0` state.

### Citations

**File:** contracts/RewardsDistributor.sol (L134-168)
```text
    function claimRewards(address account_) external override {
        claimRewards(account_, tokens);
    }

    /**
     * @notice Claim tokens accrued by account in the specified tokens
     */
    function claimRewards(address account_, IERC20[] memory tokens_) public override {
        address[] memory _accounts = new address[](1);
        _accounts[0] = account_;
        claimRewards(_accounts, tokens_);
    }

    /**
     * @notice Claim tokens accrued by the accounts in the specified tokens
     */
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
    }
```

**File:** contracts/RewardsDistributor.sol (L201-212)
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
    }
```

**File:** contracts/RewardsDistributor.sol (L222-231)
```text
        _tokenIndex = _tokenState.index;
        uint256 _accountIndex = accountIndexOf[token_][account_];

        if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
            _accountIndex = INITIAL_INDEX;
        }

        uint256 _deltaIndex = _tokenIndex - _accountIndex;
        _tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
    }
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

**File:** contracts/RewardsDistributor.sol (L261-265)
```text
    function _updateTokensAccruedOf(IERC20 token_, address account_) private {
        (uint256 _tokenIndex, uint256 _tokensDelta) = _calculateTokenDelta(tokenStates[token_], token_, account_);
        accountIndexOf[token_][account_] = _tokenIndex;
        tokensAccruedOf[account_] = tokensAccruedOf[account_] + _tokensDelta;
        emit TokensAccruedUpdated(token_, account_, _tokensDelta, _tokenIndex);
```

**File:** contracts/RewardsDistributor.sol (L296-303)
```text
            if (tokenStates[token_].index == 0) {
                if (tokens.length == MAX_REWARD_TOKENS) revert ReachedMaxRewardTokens();
                tokenStates[token_] = TokenState({index: INITIAL_INDEX, timestamp: block.timestamp.toUint32()});
                tokens.push(token_);
            } else {
                // Update timestamp to ensure extra interest is not accrued during the prior period
                tokenStates[token_].timestamp = block.timestamp.toUint32();
            }
```

**File:** contracts/RewardsDistributor.sol (L339-347)
```text
        uint256 _speed;

        if (block.timestamp < _rewards.periodFinish(address(rewardToken))) {
            _speed =
                (_rewards.rewardRates(address(rewardToken)) * _vPool.balanceOf(address(pool.treasury()))) /
                _vPool.totalSupply();
        }

        _updateTokenSpeed(IERC20(address(depositToken_)), _speed);
```
