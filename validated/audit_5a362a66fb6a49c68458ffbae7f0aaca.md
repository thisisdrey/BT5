### Title
Removed `RewardsDistributor` continues accruing and paying rewards - ([File: contracts/Pool.sol])

### Summary
`Pool.removeRewardsDistributor()` removes the distributor from the pool registry but does not stop the distributor’s reward accounting. The removed contract retains nonzero `tokenSpeeds`, token indexes, per-account indexes, and accrued balances. Because `claimRewards()` and the public index-update functions do not verify that the distributor is still registered, users continue earning rewards after removal.

### Finding Description
A governor can remove a rewards distributor through `Pool.removeRewardsDistributor()`. The function only removes the distributor address from `rewardsDistributors`; it does not call into the distributor, set its speeds to zero, or otherwise disable accrual. [1](#0-0) 

`RewardsDistributor._calculateTokenIndex()` continues increasing the token index whenever `tokenSpeeds[token_] > 0`. [2](#0-1)  `claimRewards()` updates that index, updates each account’s accrued amount, and transfers rewards without checking `onlyIfDistributorExists`. [3](#0-2) 

The `onlyIfDistributorExists` check exists and is used for speed updates, but it is absent from reward claims and public accounting updates. [4](#0-3) [5](#0-4)  Consequently, removing the distributor does not revoke its already-configured reward emission authority; its previous “votes” continue to be counted.

### Impact Explanation
Users can claim rewards attributable to periods after the distributor was removed. If the contract holds reward tokens, `claimRewards()` transfers them to users based on stale `tokenSpeeds`. [6](#0-5)  This can drain reward inventory that governance intended to stop distributing, causing theft of unclaimed yield and reducing rewards available for an active distributor or later reward program.

### Likelihood Explanation
The issue is triggered by an ordinary supported administrative operation: calling `removeRewardsDistributor()` without first setting every configured token speed to zero. No privileged attacker action is required afterward. Any account can call `claimRewards()` or the public update functions directly.

### Recommendation
Before removing a distributor, update every tracked token’s speed to zero so that accrual stops at the removal timestamp. Additionally, add `onlyIfDistributorExists` to `claimRewards()` and the public `updateBeforeMintOrBurn()`/`updateBeforeTransfer()` entry points if removal is intended to disable the distributor completely. Preserve any intentionally grandfathered accrued rewards separately, rather than allowing the stale distributor to continue indexing emissions.

### Proof of Concept
Hardhat-style reproduction:

```ts
it('removed distributor continues accruing rewards', async () => {
  const {pool, distributor, depositToken, rewardToken, governor, alice} =
    await loadFixture(deployFixture)

  // Governor configures rewards while distributor is registered.
  await pool.connect(governor).addRewardsDistributor(distributor.address)
  await distributor.connect(governor).updateTokenSpeed(
    depositToken.address,
    ethers.utils.parseEther('1')
  )
  await rewardToken.mint(distributor.address, ethers.utils.parseEther('1000'))

  // Alice obtains a tracked deposit-token balance.
  await depositToken.mint(alice.address, ethers.utils.parseEther('10'))

  // Remove distributor without zeroing tokenSpeeds.
  await pool.connect(governor).removeRewardsDistributor(distributor.address)
  expect(await pool.getRewardsDistributors()).to.not.include(distributor.address)

  // Time passes after removal.
  await ethers.provider.send('evm_increaseTime', [3600])
  await ethers.provider.send('evm_mine')

  // The removed distributor still counts post-removal emissions.
  const before = await rewardToken.balanceOf(alice.address)
  await distributor.claimRewards(alice.address, [depositToken.address])
  const after = await rewardToken.balanceOf(alice.address)

  expect(after).to.be.gt(before)
})
```

The key assertions are that the distributor is absent from `Pool.getRewardsDistributors()` but `claimRewards()` still executes and pays. The transfer succeeds because `claimRewards()` invokes `_updateTokenIndex()` and `_updateTokensAccruedOf()` without checking distributor registration. [3](#0-2)

### Citations

**File:** contracts/Pool.sol (L747-754)
```text
     * @notice Remove a RewardsDistributor contract
     */
    function removeRewardsDistributor(IRewardsDistributor distributor_) external onlyGovernor {
        if (address(distributor_) == address(0)) revert AddressIsNull();
        if (!rewardsDistributors.remove(address(distributor_))) revert RewardDistributorDoesNotExist();

        emit RewardsDistributorRemoved(distributor_);
    }
```

**File:** contracts/RewardsDistributor.sol (L73-84)
```text
    modifier onlyIfDistributorExists() {
        bool _distributorAdded = false;
        address[] memory _rewardsDistributors = pool.getRewardsDistributors();
        uint256 _length = _rewardsDistributors.length;
        for (uint256 i; i < _length; ++i) {
            if (_rewardsDistributors[i] == address(this)) {
                _distributorAdded = true;
                break;
            }
        }
        if (!_distributorAdded) revert DistributorDoesNotExist();
        _;
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

**File:** contracts/RewardsDistributor.sol (L201-208)
```text
        uint256 _speed = tokenSpeeds[token_];
        uint256 _deltaTimestamps = block.timestamp - uint256(_supplyState.timestamp);
        if (_deltaTimestamps > 0 && _speed > 0) {
            uint256 _totalSupply = token_.totalSupply();
            uint256 _tokensAccrued = _deltaTimestamps * _speed;
            uint256 _ratio = _totalSupply > 0 ? _tokensAccrued.wadDiv(_totalSupply) : 0;
            _newIndex = (_supplyState.index + _ratio).toUint224();
            _newTimestamp = block.timestamp.toUint32();
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

**File:** contracts/RewardsDistributor.sol (L287-309)
```text
    function _updateTokenSpeed(
        IERC20 token_,
        uint256 newSpeed_
    ) private onlyIfDistributorExists onlyIfTokenExists(address(token_)) {
        uint256 _currentSpeed = tokenSpeeds[token_];
        if (_currentSpeed > 0) {
            _updateTokenIndex(token_);
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

        if (_currentSpeed != newSpeed_) {
            tokenSpeeds[token_] = newSpeed_;
            emit TokenSpeedUpdated(token_, _currentSpeed, newSpeed_);
        }
```
