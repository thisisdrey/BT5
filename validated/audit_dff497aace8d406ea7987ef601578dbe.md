### Title
Zero `accountIndexOf` is treated as literal index `0` instead of `INITIAL_INDEX`, minting `balance * INITIAL_INDEX` of unearned rewards - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary

The BPF analog of the "fake_reg" bug is a transient/default object being refined through a constraint path and then written back into real state. In `RewardsDistributor`, `_calculateTokenDelta` fabricates a "fake" account index (`INITIAL_INDEX`) only when the current supply index is strictly greater than `INITIAL_INDEX`. When an account holds a token balance while its `accountIndexOf` is still `0` and the token index equals exactly `INITIAL_INDEX` (the window between `updateTokenSpeed` enabling a token and the first index growth), the fallback is skipped, `_deltaIndex` becomes `INITIAL_INDEX` (1e18), and `_updateTokensAccruedOf` permanently writes `balance * 1` reward tokens into `tokensAccruedOf[account]`.

### Finding Description

`claimRewards` and `updateBeforeMintOrBurn` are permissionless. `_updateTokensAccruedOf` calls `_calculateTokenDelta` and stores the result into `tokensAccruedOf[account_]` [1](#0-0) . The fallback that maps a missing account index to `INITIAL_INDEX` only fires when `_tokenIndex > INITIAL_INDEX` [2](#0-1) .

When the governor calls `updateTokenSpeed`/`updateTokenSpeeds` with `newSpeed_ > 0` for a fresh token, the state is initialized to `TokenState({index: INITIAL_INDEX, timestamp: block.timestamp})` [3](#0-2) . In the same block, `_calculateTokenIndex` returns `(0, 0)` because `deltaTimestamps == 0` [4](#0-3) , so `_updateTokenIndex` leaves the index at `INITIAL_INDEX`. Anyone can then call `updateBeforeMintOrBurn(token, victim)` or `claimRewards(...)` for an account whose `accountIndexOf` is `0` (all holders whose balances were set before the speed was enabled, since `updateBeforeTransfer`/`updateBeforeMintOrBurn` early-return while `index == 0` [5](#0-4) ). The delta index is `INITIAL_INDEX - 0 = 1e18`, so `tokensAccruedOf` is incremented by `balance.wadMul(1e18) = balance` — roughly one reward token per share held, before a single second of emission has elapsed.

The corrupted `tokensAccruedOf` is persistent: `_transferRewardIfEnoughTokens` pays out up to the contract's full `rewardToken` balance on any later `claimRewards` call [6](#0-5) .

### Impact Explanation

Theft of unclaimed yield / direct theft of reward-token funds held by the distributor. An attacker who holds (or flash-mints via `DepositToken.deposit`, which is permissionless) a large share balance before a reward speed is activated can back-run the governor's `updateTokenSpeed` transaction in the same block and permanently accrue `≈ balance` reward tokens, then claim the distributor's entire reward balance, starving legitimate users.

### Likelihood Explanation

- `updateBeforeMintOrBurn` and `claimRewards` are callable by anyone for arbitrary accounts [7](#0-6) .
- The trigger only requires the attacker to have a balance at the moment the governor first enables a speed for a token — a normal, expected state (depositors exist before incentives are switched on). No privileged role, oracle manipulation, or malicious external contract is needed; same-block back-running of a public governance tx suffices.
- The reward token balance is the payout cap; every unit accrued is claimable.

### Recommendation

In `_calculateTokenDelta`, apply the `INITIAL_INDEX` fallback whenever `_accountIndex == 0 && _tokenIndex >= INITIAL_INDEX` (or simply `if (_accountIndex == 0) _accountIndex = _tokenIndex`), so an uninitialized account index can never produce a nonzero `_deltaIndex`. Alternatively, in `_updateTokenSpeed`, eagerly initialize `accountIndexOf` semantics by treating `index == INITIAL_INDEX` as "no emissions yet" and skip `_updateTokensAccruedOf` until the index has grown.

### Proof of Concept

Foundry test sketch (deploy mocks per `test/foundry/DepositToken.invariants.t.sol` setup):

```solidity
function test_zeroAccountIndexDrainsRewards() public {
    // 1. Attacker deposits before any speed is set
    underlying.mint(attacker, 1_000e18);
    vm.startPrank(attacker);
    underlying.approve(address(depositToken), 1_000e18);
    depositToken.deposit(1_000e18, attacker);
    vm.stopPrank();
    // accountIndexOf[depositToken][attacker] == 0; tokenStates.index == 0

    // 2. Fund distributor with reward tokens
    rewardToken.mint(address(distributor), 500e18);

    // 3. Governor enables speed; attacker back-runs in same block
    vm.prank(governor);
    distributor.updateTokenSpeed(IERC20(address(depositToken)), 1e18);
    // still same timestamp: index == INITIAL_INDEX, deltaTimestamps == 0

    // 4. Permissionless accrual write corrupts tokensAccruedOf
    distributor.updateBeforeMintOrBurn(IERC20(address(depositToken)), attacker);
    assertEq(distributor.claimable(attacker), 1_000e18); // balance * INITIAL_INDEX

    // 5. Attacker claims, draining the distributor
    vm.warp(block.timestamp + 1 days);
    distributor.claimRewards(attacker);
    assertEq(rewardToken.balanceOf(attacker), 500e18);
    assertEq(rewardToken.balanceOf(address(distributor)), 0);
}
```

Key assertion: `tokensAccruedOf[attacker] == depositToken.balanceOf(attacker)` immediately after the same-block `updateBeforeMintOrBurn`, despite zero elapsed emission time — the "fake" default index `0` was intersected with the real index and written back as accrued rewards.

### Citations

**File:** contracts/RewardsDistributor.sol (L141-191)
```text
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

    /**
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
```

**File:** contracts/RewardsDistributor.sol (L200-211)
```text
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
