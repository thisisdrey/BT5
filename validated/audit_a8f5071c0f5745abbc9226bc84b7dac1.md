### Title
Rewards emitted while a tracked token's `totalSupply == 0` are permanently locked in `RewardsDistributor` - (File: contracts/RewardsDistributor.sol)

### Summary
`RewardsDistributor._calculateTokenIndex` distributes streaming `rewardToken` emissions pro-rata over `token_.totalSupply()`. When `totalSupply == 0`, the accrual ratio is forced to `0`, but the state's `timestamp` is still advanced. The emissions notionally emitted during the zero-supply window (`_deltaTimestamps * _speed`) are therefore credited to no one and can never be claimed — the same bug class as the Y2K report where `emissions` paid in are stranded when `finalTVL == 0`.

### Finding Description
The index update math is at `contracts/RewardsDistributor.sol:197-212`:

```solidity
uint256 _speed = tokenSpeeds[token_];
uint256 _deltaTimestamps = block.timestamp - uint256(_supplyState.timestamp);
if (_deltaTimestamps > 0 && _speed > 0) {
    uint256 _totalSupply = token_.totalSupply();
    uint256 _tokensAccrued = _deltaTimestamps * _speed;
    uint256 _ratio = _totalSupply > 0 ? _tokensAccrued.wadDiv(_totalSupply) : 0;
    _newIndex = (_supplyState.index + _ratio).toUint224();
    _newTimestamp = block.timestamp.toUint32();
}
```

When `totalSupply == 0` and `speed > 0`:
- `_ratio = 0`, so `_newIndex == _supplyState.index` — no account can ever accrue rewards for this window (`_tokensDelta = balance * (index - accountIndex)` in `_calculateTokenDelta`, lines 217-231).
- `_newTimestamp = block.timestamp` is returned, and `_updateTokenIndex` (lines 271-282) persists it because `_newIndex > 0` (index starts at `INITIAL_INDEX = 1e18`) and `_newTimestamp > 0`. The elapsed time is consumed without any accrual.

The `_tokensAccrued` amount for that interval is simply discarded. Since the only way `rewardToken` leaves the contract is `_transferRewardIfEnoughTokens`, which requires `tokensAccruedOf[account_] > 0` (lines 248-256), and no sweep/rescue function exists, those rewards are locked forever.

Zero `totalSupply` is a normal, reachable state: a `DepositToken`/`DebtToken` can be reward-enabled (`updateTokenSpeed`/`updateTokenSpeeds`/`syncTokenSpeed`, lines 315-348) while it has no holders, or all holders can withdraw/burn. `updateBeforeMintOrBurn` is publicly callable (line 175), so the timestamp advance is not even contingent on token activity.

### Impact Explanation
Permanent freezing of unclaimed yield: all `rewardToken` emitted during any period where a reward-enabled token has `totalSupply == 0` is stranded in the distributor with no recovery path, directly mirroring the referenced finding where `finalTVL == 0` locks `emissionsToken` in the vault.

### Likelihood Explanation
Conditional but realistic: it requires a non-zero `tokenSpeed` while the corresponding deposit/debt token has zero supply — e.g., emissions enabled on a newly listed or fully-exited market. Unlike the Y2K case, no attacker action is required; the loss accrues passively over wall-clock time.

### Recommendation
Do not advance `tokenStates[token_].timestamp` when `totalSupply == 0` (or when the computed `_ratio == 0` despite `_speed > 0`), so the un-accrued interval is rolled into the next period where holders exist. Alternatively, keep the timestamp advance but track undistributed emissions in a pending pool that is released on the next non-zero-supply update, or add a governor-only sweep for stranded `rewardToken`.

### Proof of Concept
Hardhat-style sketch (mirrors `test/RewardDistributor.test.ts` setup where `totalSupply` is mocked to 0):

```ts
// governor enables emissions for msdTOKEN1 while it has zero supply
await rewardDistributor.updateTokenSpeed(msdTOKEN1.address, parseEther('1'))
msdTOKEN1.totalSupply.returns(0)

// fund distributor
await vsp.mint(rewardDistributor.address, parseEther('100'))

// 10 seconds pass -> 10 VSP "emitted" but supply == 0
await increaseTimeOfNextBlock(10)
await rewardDistributor.updateBeforeMintOrBurn(msdTOKEN1.address, alice.address)

// timestamp advanced, index unchanged -> the 10 VSP is lost
const { index, timestamp } = await rewardDistributor.tokenStates(msdTOKEN1.address)
expect(timestamp).eq(await latestTimestamp())
expect(index).eq(INITIAL_INDEX) // no accrual

// even after deposits arrive, those 10 VSP are never credited to anyone
msdTOKEN1.totalSupply.returns(parseEther('100'))
msdTOKEN1.balanceOf.whenCalledWith(alice.address).returns(parseEther('100'))
await increaseTimeOfNextBlock(10)
await mine()
expect(await rewardDistributor['claimable(address)'](alice.address)).eq(parseEther('10'))
// distributor balance still holds 100 VSP; only 20 were ever emitted to users;
// the first 10 VSP are unclaimable forever
```

The key assertion: `tokenStates.timestamp` advances while `index` is unchanged when `totalSupply == 0`, proving the emissions for that window are dropped rather than deferred.