### Title

Frequent public reward checkpoints can truncate small accruals to zero - (contracts/RewardsDistributor.sol)

### Summary

`RewardsDistributor.updateBeforeMintOrBurn()` is permissionless and checkpoints both the global reward index and an arbitrary account’s reward index. Because each per-account update is rounded independently with `wadMul()`, an account whose reward during a checkpoint interval is below 0.5 reward-token wei accrues zero while its `accountIndexOf` still advances to the latest index. Repeating this call for every timestamp can permanently erase small reward entitlements that would have become nonzero under a single later checkpoint.

### Finding Description

The reward index increases by `speed * elapsedTime / totalSupply` in `_calculateTokenIndex()` [1](#0-0) . An account’s reward is then calculated as `balanceOf(account) * deltaIndex` using 18-decimal half-up rounding in `_calculateTokenDelta()` [2](#0-1) ; `wadMul()` adds `0.5e18` before division by `1e18` [3](#0-2) .

Crucially, `_updateTokensAccruedOf()` writes the new account index even when the rounded `_tokensDelta` is zero [4](#0-3) . Therefore, if `balance * deltaIndex < 0.5e18`, the account receives no accrued tokens, while the sub-unit reward represented by that index delta is discarded.

Any EOA can force this truncation because `updateBeforeMintOrBurn(token, account)` accepts an arbitrary account and has no caller validation [5](#0-4) . The same behavior can also be forced through the public `claimRewards()` path, which updates an arbitrary listed account before paying it [6](#0-5) .

### Impact Explanation

A victim’s earned reward is never added to `tokensAccruedOf[victim]`, even though the victim’s checkpoint advances past the index interval that generated it. The lost reward cannot later be recovered by that victim from the missed intervals; it remains unused in the distributor or is effectively allocated to future/other accruals. This is permanent loss of unclaimed yield for low-share accounts or low per-interval emission rates.

### Likelihood Explanation

The attack requires only public calls and no privileged role. It is limited by block frequency and requires each interval’s victim reward to round to zero, so the practical impact is greatest for small balances, large token supplies, or small configured `tokenSpeeds`. The gas cost may exceed the value destroyed when only a few wei are affected, making large sustained campaigns most plausible where repeated automated checkpointing is already economical or where the rounding loss aggregates across sufficiently many intervals.

### Recommendation

Store an unrounded remainder per account/token, or defer writing `accountIndexOf[token][account]` when the calculated `_tokensDelta` is zero. Preferably remove general-purpose public account checkpointing and expose only a global index refresh, while allowing `DepositToken` and `DebtToken` to perform their existing pre-balance-change checkpoints [7](#0-6) [8](#0-7) .

### Proof of Concept

This Hardhat test follows the repository’s existing mocked `RewardsDistributor` setup. It creates an index delta of `1` per second and gives both users `0.49e18` token balance; each one-second accrual is `0.49` reward wei and rounds to zero. Alice is force-checkpointed every second and receives nothing, while Bob is checkpointed once after ten seconds and receives five wei.

```ts
it('loses small rewards when externally checkpointed every second', async function () {
  const speed = parseEther('1')
  const balance = parseEther('0.49')
  const totalSupply = parseEther('1').mul(parseEther('1')) // 1e36

  await rewardDistributor.updateTokenSpeed(msdTOKEN1.address, speed)

  // Initialize both account checkpoints while balances are zero.
  msdTOKEN1.totalSupply.returns(0)
  msdTOKEN1.balanceOf.returns(0)
  await rewardDistributor.updateBeforeMintOrBurn(msdTOKEN1.address, alice.address)
  await rewardDistributor.updateBeforeMintOrBurn(msdTOKEN1.address, bob.address)

  msdTOKEN1.totalSupply.returns(totalSupply)
  msdTOKEN1.balanceOf.whenCalledWith(alice.address).returns(balance)
  msdTOKEN1.balanceOf.whenCalledWith(bob.address).returns(balance)

  // Each block: deltaIndex = 1; 0.49e18 * 1 rounds to 0 reward wei.
  for (let i = 0; i < 10; ++i) {
    await increaseTimeOfNextBlock(1)
    await rewardDistributor
      .connect(bob) // arbitrary unprivileged caller
      .updateBeforeMintOrBurn(msdTOKEN1.address, alice.address)
  }

  // Same timestamp: Bob sees the full accumulated index delta of 10.
  await rewardDistributor.updateBeforeMintOrBurn(msdTOKEN1.address, bob.address)

  expect(await rewardDistributor.tokensAccruedOf(alice.address)).eq(0)
  expect(await rewardDistributor.tokensAccruedOf(bob.address)).eq(5)
})
```

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

**File:** contracts/RewardsDistributor.sol (L175-180)
```text
    function updateBeforeMintOrBurn(IERC20 token_, address account_) external override {
        if (tokenStates[token_].index > 0) {
            _updateTokenIndex(token_);
            _updateTokensAccruedOf(token_, account_);
        }
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

**File:** contracts/RewardsDistributor.sol (L229-230)
```text
        uint256 _deltaIndex = _tokenIndex - _accountIndex;
        _tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```

**File:** contracts/RewardsDistributor.sol (L261-265)
```text
    function _updateTokensAccruedOf(IERC20 token_, address account_) private {
        (uint256 _tokenIndex, uint256 _tokensDelta) = _calculateTokenDelta(tokenStates[token_], token_, account_);
        accountIndexOf[token_][account_] = _tokenIndex;
        tokensAccruedOf[account_] = tokensAccruedOf[account_] + _tokensDelta;
        emit TokensAccruedUpdated(token_, account_, _tokensDelta, _tokenIndex);
```

**File:** contracts/lib/WadRayMath.sol (L25-31)
```text
    function wadMul(uint256 a, uint256 b) internal pure returns (uint256) {
        if (a == 0 || b == 0) {
            return 0;
        }

        return (a * b + HALF_WAD) / WAD;
    }
```

**File:** contracts/DepositToken.sol (L124-130)
```text
    modifier updateRewardsBeforeMintOrBurn(address account_) {
        address[] memory _rewardsDistributors = pool.getRewardsDistributors();
        uint256 _length = _rewardsDistributors.length;
        for (uint256 i; i < _length; ++i) {
            IRewardsDistributor(_rewardsDistributors[i]).updateBeforeMintOrBurn(this, account_);
        }
        _;
```

**File:** contracts/DebtToken.sol (L114-119)
```text
    modifier updateRewardsBeforeMintOrBurn(address account_) {
        address[] memory _rewardsDistributors = pool.getRewardsDistributors();
        uint256 _length = _rewardsDistributors.length;
        for (uint256 i; i < _length; ++i) {
            IRewardsDistributor(_rewardsDistributors[i]).updateBeforeMintOrBurn(this, account_);
        }
```
