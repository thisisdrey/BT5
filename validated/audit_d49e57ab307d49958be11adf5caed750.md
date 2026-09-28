### Title
Rewards emitted while a reward token's supply is zero are permanently skipped and locked in RewardsDistributor - (File: contracts/RewardsDistributor.sol)

### Summary
`RewardsDistributor._calculateTokenIndex` accrues emissions as `delta * speed / totalSupply`. When `token_.totalSupply() == 0`, the ratio is set to `0`, but the function still advances `timestamp` to `block.timestamp`. The elapsed window is therefore consumed with zero accrual: the reward tokens corresponding to that window can never be distributed to any holder, and since the contract has no sweep/recovery function, they remain locked in the distributor forever. This is the same bug class as the Wenwin `Staking` finding — emissions during a zero-supply period are lost because the index is not updated but the checkpoint still moves forward.

### Finding Description
The relevant code:

```solidity
// contracts/RewardsDistributor.sol:197-212
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
    } ...
}
```

`_updateTokenIndex` (lines 271-282) persists `_newTimestamp` even when `_ratio == 0`, so the emission period with no supply is permanently written off. This path is reached from public `updateBeforeMintOrBurn` (line 175), `updateBeforeTransfer` (line 186), and `claimRewards` (line 150) — all callable by anyone.

A zero supply is reachable in two ways, mirroring the Wenwin PoC:
1. Governor sets a speed (`updateTokenSpeed`) before any deposit/debt tokens are minted.
2. All holders of a DepositToken or DebtToken burn their balance (withdraw/repay), taking `totalSupply` to 0 while `speed > 0`.

In both cases every index update during the zero-supply window advances `timestamp` while accruing nothing, so `speed * delta` worth of `rewardToken` emissions are never allocated. Because `RewardsDistributor` has no function to recover undistributed reward tokens, they are locked permanently.

### Impact Explanation
Permanent freezing of unclaimed yield. Reward tokens funded to the distributor for emission during zero-supply windows can never be claimed by any user and cannot be recovered — the contract exposes no sweep/rescue/withdraw function (the file ends at `syncTokenSpeed`/`updateTokenSpeedKeeper` only). The lost amount is bounded only by `speed * (duration of zero-supply period)`, which can be arbitrarily large if a market stays empty for a long time or if a speed is configured before first deposit.

### Likelihood Explanation
Medium-low, matching the upstream finding's Medium severity. It requires a reward-bearing token (DepositToken or DebtToken) to have `totalSupply == 0` while `tokenSpeeds[token] > 0`. This happens naturally at market bootstrap (speed set before first deposit) or if all suppliers exit. No privileged or malicious action is needed — an unprivileged user can force the accounting write by calling `updateBeforeMintOrBurn` or `claimRewards` during the zero-supply window.

### Recommendation
When `totalSupply == 0`, do not advance `tokenStates[token].timestamp` (i.e., skip storing `_newTimestamp` when `_ratio == 0`), so the un-emitted interval is carried forward and accrued to future suppliers. Alternatively/additionally, add a governor-controlled sweep of unaccrued reward tokens, or track undistributed emissions so they can be reallocated rather than silently burned.

### Proof of Concept
Foundry-style sketch against a fork where a `RewardsDistributor` is registered on a pool with `rewardToken` funded and a non-zero speed on `depositToken`:

```solidity
// Assume: rewardToken.balanceOf(distributor) > 0,
// tokenSpeeds[depositToken] = S > 0, depositToken.totalSupply() == 0
// (either no deposits yet, or all users withdrew)

(uint224 index0, uint32 ts0) = distributor.tokenStates(depositToken);

// Zero-supply window elapses while emissions are "running"
vm.warp(block.timestamp + 30 days);

// Anyone can poke the index (function is permissionless)
distributor.updateBeforeMintOrBurn(depositToken, alice);

(uint224 index1, uint32 ts1) = distributor.tokenStates(depositToken);

assertEq(index1, index0);            // no accrual
assertEq(ts1, uint32(block.timestamp)); // but the window is consumed

// Now a user deposits -> totalSupply > 0
depositToken.mint(alice, 100e18);
vm.warp(block.timestamp + 1 days);
distributor.claimRewards(alice);

// Alice only accrues S * 1 day. The S * 30 days emitted during the
// zero-supply window is never credited to anyone and there is no
// function to withdraw it -> rewardToken is locked in the distributor.
```

Equivalent Hardhat reproduction can be built on `test/RewardDistributor.test.ts` by mocking `totalSupply` to 0 during a time increase, then asserting `tokenStates.timestamp` advanced while index stayed flat.