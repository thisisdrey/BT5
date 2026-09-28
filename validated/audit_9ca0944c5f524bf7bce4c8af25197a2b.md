### Title
Stale `INITIAL_INDEX` lets any user accrue rewards equal to full token balance, draining the RewardsDistributor - (File: contracts/RewardsDistributor.sol)

### Summary
The Compound incident was an over-distribution of reward tokens caused by a faulty distribution setup. The analogous surface in Metronome is `RewardsDistributor._calculateTokenDelta`. When a registered reward token's stored index is still equal to `INITIAL_INDEX` (1e18), a first-time account is NOT rebased to `INITIAL_INDEX` because the guard only applies when `_tokenIndex > INITIAL_INDEX`. The account therefore accrues `deltaIndex = INITIAL_INDEX`, i.e. `tokensDelta = balanceOf(account) * 1.0` — free rewards equal to their entire token balance, callable by anyone via the permissionless `updateBeforeMintOrBurn` or `claimRewards`. [1](#0-0) 

### Finding Description
`_calculateTokenDelta` contains this fallback:

```solidity
if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}
uint256 _deltaIndex = _tokenIndex - _accountIndex;
_tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```

The intent is that accounts first seen after accrual began are anchored to `INITIAL_INDEX`. But when `tokenStates[token_].index == INITIAL_INDEX` exactly, `_accountIndex` remains `0`, so `_deltaIndex = 1e18` and the account is credited `balanceOf(account)` reward tokens.

A token's stored index stays at `INITIAL_INDEX` when, after `updateTokenSpeed` registers it (`tokenStates[token_] = {index: INITIAL_INDEX, ...}`), accrual produces a zero ratio — most reliably when `token_.totalSupply() == 0` during the accrual window (`_ratio = 0`, only the timestamp advances), or when the speed is set back to 0 before supply exists. Newly registered deposit/debt tokens, or tokens whose speed was set while the pool had no supply, satisfy this.

The write path is reachable without privilege:
- `updateBeforeMintOrBurn(token_, account_)` is explicitly callable by anyone (comment at line 173).
- `claimRewards(accounts_, tokens_)` is public and calls `_updateTokensAccruedOf`, then pays out whatever is in `tokensAccruedOf` up to the distributor's balance.

Note the ordering inside `claimRewards`: `_updateTokenIndex` runs first, but if `speed == 0` or `deltaTimestamps == 0` the index is left untouched at `INITIAL_INDEX`, so the inflated delta is still recorded. [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

### Impact Explanation
Theft of unclaimed yield / protocol reward funds. An attacker deposits into the pool (or borrows, for a `DebtToken` index) to obtain a large `balanceOf`, then calls `updateBeforeMintOrBurn(token, attacker)` while the token index is still `INITIAL_INDEX`. `tokensAccruedOf[attacker]` jumps by `wadMul(balance, 1e18) = balance`. A subsequent `claimRewards([attacker],[token])` transfers out reward tokens up to the distributor's full balance (`_transferRewardIfEnoughTokens` caps at balance, so the attacker can drain the entire contract). With a `DebtToken`, `balanceOf` is interest-growing principal and can be made arbitrarily large via a flash-sized borrow, letting the attacker absorb the whole reward balance in one claim. This mirrors Compound's over-distribution: rewards are emitted far in excess of the intended rate. [6](#0-5) 

### Likelihood Explanation
Requires a registered reward token whose stored index remains `INITIAL_INDEX` — e.g. a token added while pool supply is zero, or one whose speed was set then zeroed before accrual. This is a plausible operational state (new collateral listings, reward programs activated before deposits) rather than a governance misconfiguration: the attacker supplies the trigger (`updateBeforeMintOrBurn`/`claimRewards`) and the balance. No privileged role, oracle manipulation, or reentrancy is needed; `nonReentrant` and `onlyIfTokenExists` do not block the path since only registered tokens are used and `balanceOf`/`totalSupply` are honest reads. The reward payout is bounded by the distributor's token balance, capping per-run profit but not requiring any capital beyond the deposit/borrow.

### Recommendation
Change the fallback condition to also anchor accounts when the index equals `INITIAL_INDEX`, and more generally never let `_accountIndex` be `0` for a registered token:

```solidity
if (_accountIndex == 0) {
    _accountIndex = _tokenIndex >= INITIAL_INDEX ? INITIAL_INDEX : _tokenIndex;
}
```

Equivalently, treat index `== INITIAL_INDEX` as "no accrual has occurred" and record `accountIndexOf[token][account] = INITIAL_INDEX` with zero delta. Also consider skipping index initialization until supply is nonzero so that `INITIAL_INDEX` never coexists with positive user balances.

### Proof of Concept
Hardhat fork sketch:

```ts
// Precondition: depositToken is registered in RewardsDistributor with
// tokenStates[depositToken].index == INITIAL_INDEX (e.g. speed was set while
// depositToken.totalSupply() == 0, so the index never advanced).

// 1. Attacker deposits underlying into Pool, receiving depositToken balance B.
await pool.deposit(underlying.address, amount);

// 2. Permissionless index/accrued update — credits attacker B * 1.0 rewards.
await rewardsDistributor.updateBeforeMintOrBurn(depositToken.address, attacker.address);
//    tokensAccruedOf[attacker] == B (wadMul(B, INITIAL_INDEX - 0))

// 3. Claim — drains distributor balance.
const balBefore = await rewardToken.balanceOf(attacker.address);
await rewardsDistributor.claimRewards([attacker.address], [depositToken.address]);
expect(await rewardToken.balanceOf(attacker.address)).to.be.gt(balBefore);
// expected honest accrual is ~0; attacker received up to min(B, distributorBalance)
```

Variant with a `DebtToken`: `pool.borrow(...)` gives an inflated `balanceOf` (principal × debt index), so `tokensAccruedOf` can exceed the distributor balance and the claim drains it entirely, even if the attacker's deposit-token balance is small.

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

**File:** contracts/RewardsDistributor.sol (L203-207)
```text
        if (_deltaTimestamps > 0 && _speed > 0) {
            uint256 _totalSupply = token_.totalSupply();
            uint256 _tokensAccrued = _deltaTimestamps * _speed;
            uint256 _ratio = _totalSupply > 0 ? _tokensAccrued.wadDiv(_totalSupply) : 0;
            _newIndex = (_supplyState.index + _ratio).toUint224();
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
