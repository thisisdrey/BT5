### Title
Uninitialized reward index credits a holder’s full balance when reward tracking is activated - (File: contracts/RewardsDistributor.sol)

### Summary
`RewardsDistributor._calculateTokenDelta()` treats `accountIndexOf == 0` as an uninitialized account only when the global token index is strictly greater than `INITIAL_INDEX`. When reward tracking is first activated, the global index is exactly `INITIAL_INDEX`, so an existing token holder’s index remains `0` and produces a reward delta equal to their entire tracked token balance. [1](#0-0) [2](#0-1) 

### Finding Description
Calling `_updateTokenSpeed()` with a nonzero speed for a previously untracked token initializes `tokenStates[token_].index` to `INITIAL_INDEX` and adds it to `tokens`. [3](#0-2)  In the same block, `_updateTokenIndex()` does not advance the index because the timestamp delta is zero. [4](#0-3)  `_calculateTokenDelta()` then calculates `INITIAL_INDEX - 0` because the fallback only handles `_tokenIndex > INITIAL_INDEX`, not `_tokenIndex == INITIAL_INDEX`. [1](#0-0)  Since `wadMul(balance, 1e18)` returns `balance`, the account accrues one reward unit per deposit-token or debt-token balance unit. [5](#0-4)  The public `claimRewards()` path applies this update and pays `tokensAccruedOf[account]` if the distributor has enough reward tokens. [6](#0-5) [7](#0-6) 

### Impact Explanation
An unprivileged user can deposit collateral before a reward speed is enabled, back-run the normal `updateTokenSpeed()` transaction in the same block, and immediately claim up to their entire deposit-token balance from the distributor’s reward inventory. [8](#0-7) [9](#0-8)  This drains reward tokens without corresponding elapsed reward accrual, stealing unclaimed yield intended for the tracked token holders. [10](#0-9) 

### Likelihood Explanation
The exploit requires a normal governor or keeper action to activate a nonzero speed while an attacker-controlled account already holds the tracked token, followed by an attacker transaction in the same block. [11](#0-10) [12](#0-11)  No privileged call by the attacker, malformed oracle, malicious bridge endpoint, or governance misconfiguration is required; the issue is the missing equality case in the uninitialized-index guard. [1](#0-0) 

### Recommendation
Normalize a zero account index whenever the token index is at or above `INITIAL_INDEX`, such as changing the condition to `_accountIndex == 0 && _tokenIndex >= INITIAL_INDEX`, so activation does not treat `1e18` as a historical accrued delta. [1](#0-0)  More explicitly, initialize a first-time account’s baseline to the current token index before calculating `_deltaIndex`. [13](#0-12) 

### Proof of Concept
The following Hardhat sequence demonstrates the issue using the existing deployment objects; the two transactions must share the same block timestamp.

```ts
it("credits full balance on reward activation", async () => {
  const amount = ethers.utils.parseEther("1000");

  // Attacker already holds the tracked deposit token before reward activation.
  await underlying.mint(attacker.address, amount);
  await underlying.connect(attacker).approve(depositToken.address, amount);
  await depositToken.connect(attacker).deposit(amount, attacker.address);

  // Ensure the distributor can pay the erroneous claim.
  const rewardBalance = await rewardToken.balanceOf(rewardsDistributor.address);
  expect(rewardBalance).to.be.gte(amount);

  // Put the normal speed activation and attacker claim in the same block.
  await ethers.provider.send("evm_setAutomine", [false]);

  await rewardsDistributor
    .connect(governor)
    .updateTokenSpeed(depositToken.address, ethers.utils.parseEther("1"));

  await rewardsDistributor
    .connect(attacker)
    ["claimRewards(address[],address[])"](
      [attacker.address],
      [depositToken.address]
    );

  await ethers.provider.send("evm_mine", []);

  expect(await rewardToken.balanceOf(attacker.address)).to.eq(amount);
});
```

### Citations

**File:** contracts/RewardsDistributor.sol (L134-167)
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

**File:** contracts/RewardsDistributor.sol (L217-230)
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

**File:** contracts/RewardsDistributor.sol (L287-299)
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
```

**File:** contracts/RewardsDistributor.sol (L333-348)
```text
    function syncTokenSpeed(IDepositToken depositToken_) external {
        if (_msgSender() != tokenSpeedKeeper) revert NotTokenSpeedKeeper();

        IVPool _vPool = IVPool(address(depositToken_.underlying()));
        IPoolRewardsExt _rewards = IPoolRewardsExt(_vPool.poolRewards());

        uint256 _speed;

        if (block.timestamp < _rewards.periodFinish(address(rewardToken))) {
            _speed =
                (_rewards.rewardRates(address(rewardToken)) * _vPool.balanceOf(address(pool.treasury()))) /
                _vPool.totalSupply();
        }

        _updateTokenSpeed(IERC20(address(depositToken_)), _speed);
    }
```

**File:** contracts/DepositToken.sol (L211-235)
```text
    function deposit(
        uint256 amount_,
        address onBehalfOf_
    ) external override whenNotPaused nonReentrant onlyIfDepositTokenExists returns (uint256 _deposited, uint256 _fee) {
        if (amount_ == 0) revert AmountIsZero();
        if (onBehalfOf_ == address(0)) revert BeneficiaryIsNull();

        IPool _pool = pool;
        IERC20 _underlying = underlying;
        address _msgSender = _msgSender();
        address _treasury = address(_pool.treasury());

        if (_msgSender == _treasury) revert TreasuryCanNotDeposit();

        uint256 _balanceBefore = _underlying.balanceOf(_treasury);
        _underlying.safeTransferFrom(_msgSender, _treasury, amount_);
        amount_ = _underlying.balanceOf(_treasury) - _balanceBefore;

        (_deposited, _fee) = quoteDepositOut(amount_);
        if (_fee > 0) {
            _mint(_pool.feeCollector(), _fee);
        }

        _mint(onBehalfOf_, _deposited);

```
