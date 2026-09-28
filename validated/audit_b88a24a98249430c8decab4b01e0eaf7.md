### Title
Zero-supply reward checkpoint permanently strands emissions in `RewardsDistributor` - (File: `contracts/RewardsDistributor.sol`)

### Summary
`RewardsDistributor._calculateTokenIndex()` advances `TokenState.timestamp` even when the rewarded `DepositToken` or `DebtToken` has zero total supply, while adding zero to the reward index. Any emissions elapsed during that zero-supply window are therefore consumed without becoming claimable. [1](#0-0) 

### Finding Description
`TokenState.timestamp` marks the last time rewards were accrued for a rewarded token. [2](#0-1) 

When `tokenSpeeds[token_] > 0`, `_calculateTokenIndex()` computes `deltaTimestamps * speed`, but sets `_ratio` to zero if `token_.totalSupply() == 0`; it nevertheless returns the current timestamp. [3](#0-2) 

`_updateTokenIndex()` persists that timestamp even though the index did not increase, permanently skipping the emissions for the elapsed interval. [4](#0-3) 

The vulnerable update can be triggered by the permissionless `updateBeforeMintOrBurn()` and `updateBeforeTransfer()` entry points, and `updateBeforeMintOrBurn()` is explicitly documented as callable by anyone. [5](#0-4) 

No privileged poke is required in the normal `DepositToken` lifecycle: `_burn()` invokes the reward hook before reducing supply, while `_mint()` invokes it before increasing supply, so the first deposit after a zero-supply interval itself consumes the entire skipped window. [6](#0-5) [7](#0-6) [8](#0-7) 

### Impact Explanation
Reward emissions that should have accrued during a zero-supply interval remain in the distributor balance but are never represented in `tokenStates[token_].index`, `tokensAccruedOf`, or future account claims. [9](#0-8) [10](#0-9) 

This permanently freezes unclaimed yield and breaks the reward-accrual invariant that `speed * elapsedTime` is either credited through the supply index or remains pending for later accrual. [3](#0-2) 

### Likelihood Explanation
A rewarded `DepositToken` can reach zero supply through ordinary withdrawals, including the public `withdraw()` path. [11](#0-10) [7](#0-6) 

After supply reaches zero, any unprivileged caller can advance the checkpoint directly through `updateBeforeMintOrBurn()`, and the next depositor will also advance it automatically through `_mint()` before the new supply exists. [12](#0-11) [13](#0-12) 

The issue requires an already reward-enabled token with `tokenSpeeds[token_] > 0`; no attacker privileges, governance call, oracle manipulation, or reentrancy is required. [14](#0-13) 

### Recommendation
Do not advance `TokenState.timestamp` when `token_.totalSupply() == 0`; either return zero from `_calculateTokenIndex()` for that case or make `_updateTokenIndex()` return without storing a new timestamp. [15](#0-14) [4](#0-3) 

This preserves the elapsed accrual window so the pending emissions are applied when supply returns; if rewarding the first subsequent depositor with the backlog is undesirable, explicitly account for and redirect the zero-supply emissions instead of silently discarding them. [16](#0-15) 

### Proof of Concept
Add this test to `test/RewardDistributor.test.ts`; it reproduces the accounting loss with the existing fixture and demonstrates that a permissionless checkpoint consumes ten seconds of emissions while supply is zero:

```ts
it('strands emissions while rewarded token supply is zero', async function () {
  const speed = parseEther('1')

  await vsp.mint(rewardDistributor.address, parseEther('1000'))
  await rewardDistributor.updateTokenSpeed(msdTOKEN1.address, speed)

  // Establish a reward checkpoint while supply exists.
  msdTOKEN1.totalSupply.returns(parseEther('100'))
  msdTOKEN1.balanceOf.whenCalledWith(alice.address).returns(parseEther('100'))
  await rewardDistributor.updateBeforeMintOrBurn(msdTOKEN1.address, alice.address)

  // Production equivalent: the last depositor withdraws and supply reaches zero.
  msdTOKEN1.totalSupply.returns(0)
  msdTOKEN1.balanceOf.whenCalledWith(alice.address).returns(0)

  // Ten reward units elapse, then an unprivileged caller pokes the distributor.
  await increaseTimeOfNextBlock(10)
  await rewardDistributor.connect(bob).updateBeforeMintOrBurn(msdTOKEN1.address, bob.address)

  const {index} = await rewardDistributor.tokenStates(msdTOKEN1.address)
  expect(index).eq(DEFAULT_INDEX)

  // Supply returns, but the ten elapsed units were already consumed.
  msdTOKEN1.totalSupply.returns(parseEther('100'))
  msdTOKEN1.balanceOf.whenCalledWith(bob.address).returns(parseEther('100'))

  expect(await rewardDistributor['claimable(address)'](bob.address)).eq(0)
  expect(await vsp.balanceOf(rewardDistributor.address)).gte(parseEther('10'))
})
```

The assertion succeeds because `_updateTokenIndex()` stores the new timestamp while leaving the index at `INITIAL_INDEX`, making the elapsed ten seconds unavailable to Bob after his balance and total supply return. [16](#0-15) [4](#0-3)

### Citations

**File:** contracts/RewardsDistributor.sol (L170-190)
```text
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
```

**File:** contracts/RewardsDistributor.sol (L197-210)
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

**File:** contracts/RewardsDistributor.sol (L271-280)
```text
    function _updateTokenIndex(IERC20 token_) private {
        TokenState storage _supplyState = tokenStates[token_];
        (uint224 _newIndex, uint32 _newTimestamp) = _calculateTokenIndex(_supplyState, token_);
        if (_newIndex > 0 && _newTimestamp > 0) {
            _supplyState.index = _newIndex;
            _supplyState.timestamp = _newTimestamp;
            emit TokenIndexUpdated(_newIndex, _newTimestamp);
        } else if (_newTimestamp > 0) {
            _supplyState.timestamp = _newTimestamp;
            emit TokenIndexUpdated(_supplyState.index, _newTimestamp);
```

**File:** contracts/storage/RewardsDistributorStorage.sol (L9-12)
```text
    struct TokenState {
        uint224 index; // The last updated index
        uint32 timestamp; // The timestamp of the latest index update
    }
```

**File:** contracts/DepositToken.sol (L124-129)
```text
    modifier updateRewardsBeforeMintOrBurn(address account_) {
        address[] memory _rewardsDistributors = pool.getRewardsDistributors();
        uint256 _length = _rewardsDistributors.length;
        for (uint256 i; i < _length; ++i) {
            IRewardsDistributor(_rewardsDistributors[i]).updateBeforeMintOrBurn(this, account_);
        }
```

**File:** contracts/DepositToken.sol (L406-411)
```text
    function withdraw(uint256 amount_, address to_) external override returns (uint256 _withdrawn, uint256 _fee) {
        if (to_ == address(0)) revert RecipientIsNull();
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        return _withdraw({account_: _msgSender, amount_: amount_, to_: to_});
```

**File:** contracts/DepositToken.sol (L444-453)
```text
    function _burn(address _account, uint256 _amount) private updateRewardsBeforeMintOrBurn(_account) {
        if (_account == address(0)) revert BurnFromTheZeroAddress();

        uint256 _balanceBefore = balanceOf[_account];
        if (_balanceBefore < _amount) revert BurnAmountExceedsBalance();
        uint256 _balanceAfter;
        unchecked {
            _balanceAfter = _balanceBefore - _amount;
            totalSupply -= _amount;
        }
```

**File:** contracts/DepositToken.sol (L469-476)
```text
    function _mint(
        address account_,
        uint256 amount_
    ) private onlyIfDepositTokenIsActive updateRewardsBeforeMintOrBurn(account_) {
        if (account_ == address(0)) revert MintToTheZeroAddress();

        totalSupply += amount_;
        if (totalSupply > maxTotalSupply) revert SurpassMaxDepositSupply();
```
