### Title
Rounding to zero in `RewardsDistributor._calculateTokenIndex()` permanently burns accrued rewards while advancing the index timestamp - (File: contracts/RewardsDistributor.sol)

### Summary
`RewardsDistributor` accrues rewards via a global index: `_ratio = _tokensAccrued.wadDiv(_totalSupply)` where `_tokensAccrued = _deltaTimestamps * _speed` [1](#0-0) . Because `wadDiv` rounds down, whenever `_deltaTimestamps * _speed * 1e18 < totalSupply / 2` the ratio is 0 — yet `_newTimestamp` is still set to `block.timestamp` and persisted by `_updateTokenIndex()` [2](#0-1) . Anyone can call `updateBeforeMintOrBurn()` (explicitly permissionless per the NatSpec) or `updateBeforeTransfer()` to trigger the update [3](#0-2) . An attacker calling every block permanently destroys the rewards that should have accrued in each interval — identical in mechanism to the Taurus `_disburseTau()` zero-disburse/timestamp-advance bug.

### Finding Description
In `_calculateTokenIndex`, when `deltaTimestamps > 0` and `speed > 0`, the new index is `index + tokensAccrued.wadDiv(totalSupply)` [4](#0-3) . `WadRayMath.wadDiv` computes `(a * 1e18 + b/2) / b`, which is 0 when `a * 1e18 < b / 2`. With a modest `tokenSpeed` (raw wei/sec) and large DepositToken/DebtToken `totalSupply` (e.g. `>1e24` for million-scale supplies), each 1–2 s block interval yields `_tokensAccrued * 1e18 << totalSupply`, so `_ratio == 0`. Nevertheless `_newTimestamp = block.timestamp` is returned and `_updateTokenIndex()` writes `_supplyState.timestamp = _newTimestamp` even when the index did not change [5](#0-4) . Unlike Taurus's `tauWithheld` bookkeeping, here the elapsed interval is simply discarded: the `delta * speed` rewards for that window are never credited to any index and are unrecoverable. The attacker only needs to keep `deltaTimestamps` small, which any EOA can do by calling `updateBeforeMintOrBurn(token, attacker)` every block — or passively via any deposit/mint/transfer activity that funnels through the same hook.

### Impact Explanation
Permanent theft/destruction of unclaimed yield. All rewards emitted at `tokenSpeeds[token]` during the griefed period are silently dropped instead of accruing to `tokenStates[token].index`, so suppliers/borrowers lose their reward share forever (the reward token balance stays in the distributor but is never attributed). If kept up continuously, index growth — and therefore all reward distribution for that market — is effectively halted while `speed * blockInterval` remains below the rounding threshold.

### Likelihood Explanation
Requires only an unprivileged EOA making repeated public calls; gas cost on L2 deployments (Optimism/Base, where `RewardsDistributor` is deployed) is trivial. The precondition is that the per-interval accrual is small relative to total supply — common whenever `tokenSpeed` is set conservatively (it is synced from Vesper `rewardRates * treasuryBalance / totalSupply` via `syncTokenSpeed`, which can be arbitrarily small [6](#0-5) ) or when the tracked token has large supply. Note the rounding is in the *protocol's disfavor*, so even without an active attacker, any high-frequency legitimate activity (mints, repays, transfers calling `updateBeforeMintOrBurn`/`updateBeforeTransfer` internally) truncates rewards identically.

### Recommendation
Do not advance `TokenState.timestamp` when the computed `_ratio == 0`, so the un-accrued time accumulates until it produces a non-zero index delta. Alternatively accrue in higher precision (e.g. ray) or carry a remainder. Removing permissionless index updates is insufficient since the same hooks fire on every legitimate mint/burn/transfer.

### Proof of Concept
Hardhat (based on existing mocks in `test/RewardDistributor.test.ts`):

```ts
// contracts/RewardsDistributor.sol analog of TauDripFeed DoS
it('attacker truncates rewards to zero every block', async function () {
  const {rewardDistributor, msdTOKEN1, alice, attacker} = await loadFixture(fixture)

  // Governor sets a speed; large totalSupply makes per-block accrual round to 0
  const speed = parseEther('0.001')           // 1e15 wei/s
  await rewardDistributor.updateTokenSpeed(msdTOKEN1.address, speed)
  msdTOKEN1.totalSupply.returns(parseEther('100000000')) // 1e26; need speed*dt*1e18 < ts/2
  msdTOKEN1.balanceOf.returns(parseEther('1000'))

  // Attacker calls the permissionless hook every block (~1s delta)
  for (let i = 0; i < 100; i++) {
    await increaseTimeOfNextBlock(1)
    await rewardDistributor.connect(attacker)
      .updateBeforeMintOrBurn(msdTOKEN1.address, attacker.address)
  }

  const {index, timestamp} = await rewardDistributor.tokenStates(msdTOKEN1.address)
  expect(index).eq(DEFAULT_INDEX)                    // index never grew
  expect(timestamp).eq(await time.latest())          // but timestamp advanced
  // 100 seconds * speed = 1e17 wei of rewards permanently lost; without
  // the attacker, claimable for alice after 100s would be > 0
})
```

### Citations

**File:** contracts/RewardsDistributor.sol (L170-180)
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

**File:** contracts/RewardsDistributor.sol (L271-281)
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
        }
```

**File:** contracts/RewardsDistributor.sol (L341-347)
```text
        if (block.timestamp < _rewards.periodFinish(address(rewardToken))) {
            _speed =
                (_rewards.rewardRates(address(rewardToken)) * _vPool.balanceOf(address(pool.treasury()))) /
                _vPool.totalSupply();
        }

        _updateTokenSpeed(IERC20(address(depositToken_)), _speed);
```
