### Title
RewardsDistributor credits a full-balance reward when the token index has never advanced past `INITIAL_INDEX` — ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
`_calculateTokenDelta` treats `accountIndexOf[token][account] == 0` as "never synced" and substitutes `INITIAL_INDEX` as the baseline only when the global token index is strictly greater than `INITIAL_INDEX`. When the global index is still exactly `INITIAL_INDEX`, the subtraction `tokenIndex - accountIndex` yields `1e18`, so the account is credited `balanceOf(account) * 1e18` (wad) = its entire deposit/debt balance as claimable rewards. The public, unauthenticated `updateBeforeMintOrBurn` and `claimRewards` paths both trigger this, letting an unprivileged attacker drain the distributor's `rewardToken` balance.

### Finding Description
In `contracts/RewardsDistributor.sol`:

```solidity
uint256 _accountIndex = accountIndexOf[token_][account_];

if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}

uint256 _deltaIndex = _tokenIndex - _accountIndex;
_tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
``` [1](#0-0) 

The `INITIAL_INDEX` fallback covers the "existing holder, reward newly added" case, but only when `_tokenIndex > INITIAL_INDEX`. A token is registered with `tokenStates[token_] = TokenState({index: INITIAL_INDEX, ...})` the first time its speed is set to a positive value ( [2](#0-1) ). The index only advances in `_calculateTokenIndex` when `deltaTimestamps > 0 && speed > 0` *and* `_tokensAccrued.wadDiv(totalSupply)` is non-zero ( [3](#0-2) ). Two durable states keep `tokenStates[token].index == INITIAL_INDEX`:

1. The same block/timestamp in which `updateTokenSpeed` first registers the token (`deltaTimestamps == 0`).
2. Any period where `speed == 0` — `_calculateTokenIndex` then only bumps the timestamp and the index stays pinned at `INITIAL_INDEX` indefinitely. Note `_updateTokenSpeed` skips `_updateTokenIndex` when `_currentSpeed == 0`, and if the governor later sets speed back to `0`, the index remains `INITIAL_INDEX` forever for that token.
3. A rounding-degenerate regime where `tokensAccrued * 1e18 < totalSupply`, so `_ratio` truncates to 0 and the index never leaves `INITIAL_INDEX`.

This is the analog of the ImageMagick over-read: a cached index ("virtual pixel view") is read with the wrong baseline, so the delta computation reads one full `INITIAL_INDEX` worth of accrual that was never earned.

Attack path (all unprivileged, public entry points):
- `RewardsDistributor.updateBeforeMintOrBurn(token, attacker)` — explicitly documented as callable by anyone — calls `_updateTokenIndex` then `_updateTokensAccruedOf`, writing `tokensAccruedOf[attacker] += depositToken.balanceOf(attacker)` when the index is `INITIAL_INDEX` ( [4](#0-3) ).
- `RewardsDistributor.claimRewards(attacker, [token])` does the same and then calls `_transferRewardIfEnoughTokens`, which transfers `rewardToken` up to the contract's balance ( [5](#0-4) , [6](#0-5) ).

Since `accountIndexOf` is set to `_tokenIndex` afterward ( [7](#0-6) ), the credit is once per account per token, but any number of fresh accounts can each claim `balance` worth of rewards until the distributor's `rewardToken` inventory is exhausted. The attacker can simply deposit a large amount of collateral (or mint debt shares) to maximize `balanceOf(attacker)`; a flash loan into the underlying `DepositToken` supply does not help because the delta is proportional to the attacker's own balance, but an attacker with genuine deposits gets rewarded their full balance in reward tokens. Even better, multiple sybil-ish fresh accounts are unnecessary — a single account whose `balanceOf` equals or exceeds the distributor's reward balance drains it entirely.

`nonReentrant` does not help (no reentrancy needed), `onlyIfDistributorExists`/`onlyIfTokenExists` are satisfied for the production distributor and real deposit/debt tokens, and there is no pause check on `claimRewards`/`updateBeforeMintOrBurn`.

### Impact Explanation
Theft of unclaimed yield / direct theft of protocol-held funds: the entire `rewardToken` balance held by the `RewardsDistributor` (rewards owed to legitimate suppliers and borrowers) can be transferred to an attacker for a reward accrual of exactly zero elapsed emission. The reward-conservation invariant — `tokensAccruedOf` must only grow by `balance * (tokenIndex - accountIndex)` for genuinely accrued deltas — is broken because a zero `accountIndex` is read against a global index that never moved off the sentinel.

### Likelihood Explanation
High in the `speed == 0` and same-block cases, which are persistent states, not races:

- Whenever a token's speed is `0` (e.g., rewards temporarily disabled for a deposit token after being enabled once — a normal operational action via `updateTokenSpeed`), `tokenStates[token].index` remains `INITIAL_INDEX` for the whole period and every account with a zero `accountIndexOf` can claim `balanceOf(account)` in reward tokens.
- In the same block that a governor transaction first enables a speed, any caller can front-run/back-run `updateTokenSpeed` with `claimRewards` on a pre-existing position.
- Only requirement: `accountIndexOf[token][account] == 0`, which is true for any account that has not yet been accrued against that token — including accounts that held deposits since before the token was registered, and any freshly funded attacker account.

No privileged role, oracle manipulation, or malicious endpoint is needed; the trigger is a single public call.

### Recommendation
In `_calculateTokenDelta`, default a zero `accountIndexOf` to the *current* token index rather than to `INITIAL_INDEX` only when the index has advanced, e.g.:

```solidity
if (_accountIndex == 0) {
    _accountIndex = _tokenIndex > INITIAL_INDEX ? _tokenIndex : INITIAL_INDEX;
}
```

i.e., never compute a delta against index `0`; when the stored account index is unset, baseline it at the current supply index (`_tokenIndex`) so a first-time sync accrues zero delta. Equivalently, initialize `accountIndexOf[token][account]` to `tokenStates[token].index` on first touch inside `_updateTokensAccruedOf`.

### Proof of Concept
Hardhat/foundry fork sketch against the deployed `RewardsDistributor` and a registered `DepositToken` (or with the repo's mocks):

```solidity
// Setup: rewardToken funded into distributor, depositToken registered.
// Governor (test prank) enables then disables speed, leaving index == INITIAL_INDEX:
vm.prank(governor);
distributor.updateTokenSpeed(depositToken, 1);      // tokenStates.index = 1e18
vm.prank(governor);
distributor.updateTokenSpeed(depositToken, 0);      // speed 0 -> index stays 1e18 forever

// Attacker: any account holding depositToken shares with accountIndexOf == 0
uint256 shares = depositToken.balanceOf(attacker);  // > 0
assertEq(distributor.accountIndexOf(depositToken, attacker), 0);

// Trigger: public entry point
vm.prank(attacker);
distributor.claimRewards(attacker, toTokens(depositToken));

// Result: tokensAccruedOf[attacker] == shares (wad delta of 1e18),
// and rewardToken transferred to attacker up to distributor balance.
assertEq(rewardToken.balanceOf(attacker), min(shares, distributorRewardBalance));
```

Same result in a single block by sandwiching `updateTokenSpeed(depositToken, s)` with `claimRewards` before `block.timestamp` advances.

### Citations

**File:** contracts/RewardsDistributor.sol (L150-168)
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

**File:** contracts/RewardsDistributor.sol (L222-230)
```text
        _tokenIndex = _tokenState.index;
        uint256 _accountIndex = accountIndexOf[token_][account_];

        if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
            _accountIndex = INITIAL_INDEX;
        }

        uint256 _deltaIndex = _tokenIndex - _accountIndex;
        _tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
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

**File:** contracts/RewardsDistributor.sol (L261-266)
```text
    function _updateTokensAccruedOf(IERC20 token_, address account_) private {
        (uint256 _tokenIndex, uint256 _tokensDelta) = _calculateTokenDelta(tokenStates[token_], token_, account_);
        accountIndexOf[token_][account_] = _tokenIndex;
        tokensAccruedOf[account_] = tokensAccruedOf[account_] + _tokensDelta;
        emit TokensAccruedUpdated(token_, account_, _tokensDelta, _tokenIndex);
    }
```

**File:** contracts/RewardsDistributor.sol (L294-303)
```text
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
```
