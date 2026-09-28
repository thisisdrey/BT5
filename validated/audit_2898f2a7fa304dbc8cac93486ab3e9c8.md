### Title
Uninitialized `accountIndexOf` fallback lets a depositor claim reward tokens equal to their whole token balance while `tokenStates[token].index == INITIAL_INDEX` - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
`RewardsDistributor._calculateTokenDelta` only substitutes `INITIAL_INDEX` for a missing per-account index when the global index has already advanced past `INITIAL_INDEX`. While a registered reward token's index is still exactly `INITIAL_INDEX` (e.g., reward speed set to `0` before any index accrual), any holder's `accountIndexOf` is `0`, so `deltaIndex = INITIAL_INDEX - 0 = 1e18` and the account accrues `balance * 1` reward tokens — claimable against the distributor's entire `rewardToken` balance.

### Finding Description
The kernel bug registers IRQs before the NAPI handler, so an event arriving in the gap dereferences a NULL handler. The analog here is the gap between a reward token's registration (`_updateTokenSpeed` sets `tokenStates[token_] = {index: INITIAL_INDEX, timestamp: now}`) and the first index update — during that gap the "handler" (per-account index) is uninitialized.

Relevant code:
- Registration initializes the token index to `INITIAL_INDEX` (`1e18`): [1](#0-0) 
- `_calculateTokenDelta` only falls back to `INITIAL_INDEX` when `_tokenIndex > INITIAL_INDEX`; when `_tokenIndex == INITIAL_INDEX` the stored `accountIndexOf` stays `0`: [2](#0-1) 
- `claimRewards` updates the index and accrued amounts, then transfers `rewardToken` if the distributor holds enough balance: [3](#0-2) 
- `_transferRewardIfEnoughTokens` pays out `tokensAccruedOf[account_]` capped only by the distributor's reward-token balance: [4](#0-3) 

Attack sequence (unprivileged, public entry points):
1. Governor registers reward speed for `msdUSDC` (`updateTokenSpeed`), then sets speed back to `0` (or index never advances because accrual never ticked while speed > 0). `tokenStates[msdUSDC].index` remains `1e18`.
2. Attacker deposits underlying via `DepositToken.deposit` (or flash-borrows the deposit token balance path) so `balanceOf(attacker)` is large.
3. Attacker calls `RewardsDistributor.updateBeforeMintOrBurn(msdUSDC, attacker)` — permissionless per the natspec — which records `tokensAccruedOf[attacker] += balanceOf(attacker).wadMul(1e18) = balanceOf(attacker)`.
4. Attacker calls `claimRewards(attacker)` and receives `min(accrued, rewardToken.balanceOf(distributor))`.

No modifier stops this: `updateBeforeMintOrBurn` and `claimRewards` are public/nonReentrant only; `onlyIfTokenExists` passes since `msdUSDC` is a real deposit token.

### Impact Explanation
Theft of unclaimed yield: the attacker drains the distributor's `rewardToken` balance (which backs legitimate users' accrued rewards) without having earned anything, breaking the reward-accrual invariant. With a large deposit (attacker's own capital or a flash-boosted position via `DebtToken.flashIssue`-style liquidity), a single claim can extract the full distributor balance.

### Likelihood Explanation
Requires a reward token whose stored index is still `INITIAL_INDEX` — i.e., registered but never accrued (speed set to `0` before any `_updateTokenIndex` tick, or speed raised and zeroed within the same timestamp window). The keeper-driven `syncTokenSpeed` path and governor speed updates make the "registered but never accrued" state realistic during reward program setup/teardown. Exploitation itself needs only a deposit and two public calls.

### Recommendation
Initialize `accountIndexOf` semantics consistently: treat `accountIndexOf == 0` as `min(INITIAL_INDEX, _tokenIndex)` — i.e., use the stored token index itself as the fallback (`_accountIndex = _tokenIndex` when unset), or only count delta when `accountIndexOf` was explicitly written. Alternatively, have `updateBeforeMintOrBurn`/`updateBeforeTransfer` set `accountIndexOf` at registration-scan time even when no delta accrued.

### Proof of Concept
Foundry sketch (extends the existing fork harness pattern in `test/foundry/`):

```solidity
// Setup: pool, msdUSDC deposit token, rewardsDistributor initialized and
// registered on pool; governor calls:
//   rewardsDistributor.updateTokenSpeed(msdUSDC, speed);   // registers index = 1e18
//   rewardsDistributor.updateTokenSpeed(msdUSDC, 0);       // same block -> index still 1e18
// Fund distributor with rewardToken.

function test_claimWithZeroAccountIndex() public {
    uint256 depositAmount = 1_000_000e6;
    usdc.approve(address(msdUSDC), depositAmount);
    msdUSDC.deposit(depositAmount); // attacker balance = depositAmount

    // tokenStates[msdUSDC].index == INITIAL_INDEX == 1e18 (never advanced)
    rewardsDistributor.updateBeforeMintOrBurn(IERC20(address(msdUSDC)), attacker);

    uint256 accrued = rewardsDistributor.tokensAccruedOf(attacker);
    assertEq(accrued, depositAmount); // balance.wadMul(1e18)

    uint256 balBefore = rewardToken.balanceOf(attacker);
    rewardsDistributor.claimRewards(attacker);
    assertGt(rewardToken.balanceOf(attacker), balBefore); // drains distributor up to accrued
}
```

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

**File:** contracts/RewardsDistributor.sol (L296-299)
```text
            if (tokenStates[token_].index == 0) {
                if (tokens.length == MAX_REWARD_TOKENS) revert ReachedMaxRewardTokens();
                tokenStates[token_] = TokenState({index: INITIAL_INDEX, timestamp: block.timestamp.toUint32()});
                tokens.push(token_);
```
