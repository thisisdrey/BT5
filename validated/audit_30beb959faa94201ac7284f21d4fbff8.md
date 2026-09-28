### Title
RewardsDistributor `_calculateTokenDelta` uses `>` instead of `>=` for the initial-index fallback, letting a same-block `claimRewards` call credit a holder their entire token balance as rewards - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
`_calculateTokenDelta` is supposed to give a new account a starting index of `INITIAL_INDEX` so it only accrues rewards going forward. Instead it only applies the fallback when the stored token index is **strictly greater** than `INITIAL_INDEX`. When a reward token's speed is first enabled, `tokenStates[token_].index == INITIAL_INDEX` exactly, so any account whose `accountIndexOf` is still `0` computes `_deltaIndex = INITIAL_INDEX - 0 = 1e18` and is credited `balance * 1e18 * 1e-18 = balance` reward tokens. Because `claimRewards(accounts, tokens)` is permissionless and takes arbitrary account addresses, an attacker holding a large deposit/debt-token balance can drain the distributor's whole `rewardToken` balance in the same block that the speed is activated.

### Finding Description
The bug is the same class as the vhost `vhost_get_avail_idx` flaw: an index comparison against the wrong baseline misreports state, so the accounting path treats "nothing accrued yet" as "accrued everything since index 0".

- `_calculateTokenDelta` only substitutes `INITIAL_INDEX` for a zero `accountIndexOf` when `_tokenIndex > INITIAL_INDEX` (strict inequality) — the equal case falls through with `_accountIndex = 0`. [1](#0-0) 
- `_updateTokenSpeed` initializes `tokenStates[token_]` to `{index: INITIAL_INDEX, timestamp: block.timestamp}` when a token first gets a nonzero speed. [2](#0-1) 
- `updateBeforeMintOrBurn`/`updateBeforeTransfer` skip updating `accountIndexOf` entirely while `tokenStates[token_].index == 0`, so accounts that deposited/borrowed before the reward token is enabled keep `accountIndexOf == 0`. [3](#0-2) 
- `claimRewards` is `public`, `nonReentrant`, and iterates caller-supplied `accounts_`, calling `_updateTokensAccruedOf` on each. [4](#0-3) 
- In the same block as `updateTokenSpeed`, `_calculateTokenIndex` returns `_newIndex = 0` because `_deltaTimestamps == 0`, so the stored index remains exactly `INITIAL_INDEX`. [5](#0-4) 
- `_updateTokensAccruedOf` then adds `tokensDelta = balance * 1e18` scaled by `wadMul` (i.e., `= balance`) to `tokensAccruedOf[account_]`, and `_transferRewardIfEnoughTokens` pays out as long as `amount_ <= rewardToken balance`. [6](#0-5) 

This is the well-known Compound distributor "initial index" bug pattern (Compound uses `supplyIndex >= compInitialIndex` for the zero-accountIndex fallback); here the strict `>` leaves exactly one exploitable window per reward token.

### Impact Explanation
Theft of unclaimed yield / direct theft: the attacker is credited reward tokens equal to their full deposit-token (or debt-token) balance, far exceeding any legitimately accrued amount, and `claimRewards` transfers real `rewardToken` from the distributor up to its entire balance. Using flash-borrowed or leveraged size (e.g., via `SmartFarmingManager.leverage` or a same-block deposit through `Pool.deposit`/`NativeTokenGateway.deposit`), the attacker can make `balance` large enough to drain the distributor in one call.

### Likelihood Explanation
The window is narrow but deterministic: it exists only in the block where a reward token's speed transitions from `0` to nonzero while some account holds tokens with `accountIndexOf[token][account] == 0`. An unprivileged attacker can pre-position by holding deposit tokens before `updateTokenSpeed` is executed (visible in the mempool / public governor transactions), depositing in the same block via `Operator.execute` batching, or back-running `updateTokenSpeed` with `claimRewards(attacker, [token])` before the timestamp advances. `syncTokenSpeed` (callable by `tokenSpeedKeeper`) also sets initial speeds and creates the same window. The `nonReentrant` guard does not help because the exploit is a single atomic call, and `onlyIfTokenExists`/`onlyIfDistributorExists` pass for legitimately registered reward tokens.

### Recommendation
Change the fallback to use `>=` so the equality case is covered, matching the Compound-style fix:

```solidity
// contracts/RewardsDistributor.sol, in _calculateTokenDelta
if (_accountIndex == 0 && _tokenIndex >= INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}
```

Alternatively, seed `accountIndexOf[token_][account_] = INITIAL_INDEX` whenever `tokenStates[token_].index` transitions from `0` to `INITIAL_INDEX` for interacting accounts, and add a regression test that calls `claimRewards` in the same block as `updateTokenSpeed` and asserts `tokensDelta == 0`.

### Proof of Concept
Hardhat sketch (fork or local deployment with `Pool`, `DepositToken`, `RewardsDistributor`, and a funded `rewardToken`):

```solidity
// test/RewardsDistributor.initialIndex.t.sol
// Precondition: attacker holds D deposit tokens BEFORE any reward speed is set,
// so accountIndexOf[depositToken][attacker] == 0.

function testStealRewardsOnSpeedActivation() public {
    // 1) Attacker deposits (or flash-borrows and deposits) while index == 0.
    //    updateBeforeMintOrBurn is a no-op because tokenStates[dToken].index == 0.
    uint256 bal = dToken.balanceOf(attacker);
    assertEq(distributor.accountIndexOf(address(dToken), attacker), 0);

    // Fund distributor with rewardToken (pre-existing accrued rewards).
    deal(address(rewardToken), address(distributor), REWARD_POOL);

    // 2) Same block: governor sets speed -> index = INITIAL_INDEX = 1e18.
    vm.prank(governor);
    distributor.updateTokenSpeed(IERC20(address(dToken)), 1e18); // speed > 0

    // 3) Attacker calls claimRewards in the same block (same timestamp).
    //    _calculateTokenIndex: _deltaTimestamps == 0 -> index stays INITIAL_INDEX.
    //    _calculateTokenDelta: accountIndex==0 but index !> INITIAL_INDEX
    //        -> deltaIndex = 1e18 -> tokensDelta = bal.wadMul(1e18) = bal.
    vm.prank(attacker);
    IERC20[] memory toks = new IERC20[](1);
    toks[0] = IERC20(address(dToken));
    distributor.claimRewards(attacker, toks);

    // Attacker received `bal` reward tokens (up to distributor balance) for 0 elapsed time.
    assertEq(rewardToken.balanceOf(attacker), min(bal, REWARD_POOL));
}
```

A Foundry variant should additionally use `vm.roll`/`vm.warp` to prove the exploit fails once `block.timestamp` advances past the activation block (the strict-`>` branch then correctly assigns `INITIAL_INDEX`), confirming the bug is confined to the equality window.

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

**File:** contracts/RewardsDistributor.sol (L175-191)
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

**File:** contracts/RewardsDistributor.sol (L223-230)
```text
        uint256 _accountIndex = accountIndexOf[token_][account_];

        if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
            _accountIndex = INITIAL_INDEX;
        }

        uint256 _deltaIndex = _tokenIndex - _accountIndex;
        _tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```

**File:** contracts/RewardsDistributor.sol (L248-264)
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

    /**
     * @notice Calculate tokens accrued by an account
     */
    function _updateTokensAccruedOf(IERC20 token_, address account_) private {
        (uint256 _tokenIndex, uint256 _tokensDelta) = _calculateTokenDelta(tokenStates[token_], token_, account_);
        accountIndexOf[token_][account_] = _tokenIndex;
        tokensAccruedOf[account_] = tokensAccruedOf[account_] + _tokensDelta;
```

**File:** contracts/RewardsDistributor.sol (L296-299)
```text
            if (tokenStates[token_].index == 0) {
                if (tokens.length == MAX_REWARD_TOKENS) revert ReachedMaxRewardTokens();
                tokenStates[token_] = TokenState({index: INITIAL_INDEX, timestamp: block.timestamp.toUint32()});
                tokens.push(token_);
```
