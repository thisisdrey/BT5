### Title
Default reward index is treated as zero, letting users instantly claim unearned rewards - (File: `contracts/RewardsDistributor.sol`)

### Summary
`RewardsDistributor._calculateTokenDelta()` fails to distinguish the initial token index `INITIAL_INDEX = 1e18` from an uninitialized account index `0`. When a token’s global index is exactly `INITIAL_INDEX`, a user’s first reward update accrues `balance * 1e18` instead of zero rewards.

### Finding Description
When rewards are first enabled for a debt or deposit token, `_updateTokenSpeed()` initializes `tokenStates[token_]` to `TokenState({index: INITIAL_INDEX, timestamp: block.timestamp})` and adds the token to `tokens`. [1](#0-0) 

For an account whose `accountIndexOf[token_][account_] == 0`, `_calculateTokenDelta()` substitutes `INITIAL_INDEX` only when `_tokenIndex > INITIAL_INDEX`. [2](#0-1)  At equality, `_deltaIndex = INITIAL_INDEX - 0`, so `_tokensDelta = token_.balanceOf(account_).wadMul(1e18)`, i.e. the account’s entire token balance is credited as reward entitlement. [3](#0-2) 

An attacker reaches this path through public calls:

1. Deposit collateral through `DepositToken.deposit()` or otherwise acquire a positive deposit-token balance.
2. After the reward token is initialized for that deposit token, call `RewardsDistributor.updateBeforeMintOrBurn(depositToken, attacker)`; the token state is nonzero, so the function updates the attacker’s accrued rewards. [4](#0-3) 
3. Call `RewardsDistributor.claimRewards(attacker, [depositToken])`, which updates and then transfers `tokensAccruedOf[attacker]` if the distributor has enough reward tokens. [5](#0-4) 
4. `_transferRewardIfEnoughTokens()` zeroes the accrued balance and transfers the reward token to the attacker. [6](#0-5) 

This is analogous to accepting a reserved sentinel value as an ordinary bound identity: account index `0` means “uninitialized,” but the check only handles it when the global index is strictly greater than `INITIAL_INDEX`, leaving the boundary case unhandled.

### Impact Explanation
The attacker can immediately claim reward tokens equal in raw units to their deposit-token or debt-token balance, without waiting for rewards to accrue. The attacker can scale the theft by depositing a large amount or repeatedly using additional accounts while the affected token index remains `INITIAL_INDEX`. This drains unclaimed yield held by the distributor and constitutes direct theft of reward funds. [7](#0-6) 

### Likelihood Explanation
The attack requires `tokenStates[token].index == INITIAL_INDEX`, which occurs when a reward token is first added with a nonzero speed and before its index advances. It can also occur if a token state exists at `INITIAL_INDEX` with zero speed. Any unprivileged account with a positive balance in that token can trigger accrual through `updateBeforeMintOrBurn()` and collect through the permissionless `claimRewards()` path; no privileged call by the attacker is required. Once the global index has advanced beyond `INITIAL_INDEX`, the existing fallback assigns new accounts `INITIAL_INDEX`, so the issue is limited to configurations containing a token still at the initial index. [8](#0-7) 

### Recommendation
Treat an uninitialized account index as `INITIAL_INDEX` whenever the token index is at least `INITIAL_INDEX`, for example:

```solidity
if (_accountIndex == 0 && _tokenIndex >= INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}
```

Alternatively, initialize `accountIndexOf` explicitly for all accounts when token reward tracking is initialized. Add regression coverage for the boundary condition `_tokenIndex == INITIAL_INDEX` and `accountIndex == 0`.

### Proof of Concept
A Foundry test can reproduce the issue against the production contracts with mocked pool/token dependencies:

```solidity
function testInitialIndexAccruesInstantRewards() public {
    // Reward distributor is registered in Pool and rewardToken is funded.
    rewardsDistributor.initialize(pool, rewardToken);

    // Governor activates rewards for depositToken.
    vm.prank(governor);
    rewardsDistributor.updateTokenSpeed(IERC20(address(depositToken)), 1);

    // Attacker holds/mints deposit-token balance.
    depositToken.mint(attacker, 100 ether);
    rewardToken.mint(address(rewardsDistributor), 1_000 ether);

    // Public update credits attacker balance * INITIAL_INDEX.
    vm.prank(attacker);
    rewardsDistributor.updateBeforeMintOrBurn(
        IERC20(address(depositToken)),
        attacker
    );

    assertEq(rewardsDistributor.claimable(attacker), 100 ether);

    vm.prank(attacker);
    rewardsDistributor.claimRewards(attacker);

    assertEq(rewardToken.balanceOf(attacker), 100 ether);
}
```

The key assertion is that `claimable(attacker)` is `100 ether` immediately after the update, despite zero elapsed reward time, because `INITIAL_INDEX - 0` produces a full `1e18` index delta. [9](#0-8)

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

**File:** contracts/RewardsDistributor.sol (L175-179)
```text
    function updateBeforeMintOrBurn(IERC20 token_, address account_) external override {
        if (tokenStates[token_].index > 0) {
            _updateTokenIndex(token_);
            _updateTokensAccruedOf(token_, account_);
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

**File:** contracts/RewardsDistributor.sol (L294-299)
```text
        } else if (newSpeed_ > 0) {
            // Add token to the list
            if (tokenStates[token_].index == 0) {
                if (tokens.length == MAX_REWARD_TOKENS) revert ReachedMaxRewardTokens();
                tokenStates[token_] = TokenState({index: INITIAL_INDEX, timestamp: block.timestamp.toUint32()});
                tokens.push(token_);
```
