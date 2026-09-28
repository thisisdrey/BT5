### Title
Rewards accrued while a rewarded token has zero supply are permanently lost — ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
`RewardsDistributor._calculateTokenIndex` advances `tokenStates[token].timestamp` even when `token_.totalSupply() == 0`, while adding `0` to the index. Every reward token that should have accrued during the zero-supply window (`deltaTimestamp * tokenSpeeds[token]`) is never attributed to the index, so it can never be claimed by anyone. Since the contract has no sweep/recovery function for `rewardToken`, those rewards are stranded forever. This is the same bug class as Blend's M-07: emission state is advanced while supply is zero, permanently burning the period's emissions.

### Finding Description
In `contracts/RewardsDistributor.sol:197-212`:

```solidity
if (_deltaTimestamps > 0 && _speed > 0) {
    uint256 _totalSupply = token_.totalSupply();
    uint256 _tokensAccrued = _deltaTimestamps * _speed;
    uint256 _ratio = _totalSupply > 0 ? _tokensAccrued.wadDiv(_totalSupply) : 0;
    _newIndex = (_supplyState.index + _ratio).toUint224();
    _newTimestamp = block.timestamp.toUint32();
}
```

When `_totalSupply == 0`, `_ratio` is `0` but `_newTimestamp` is still set to `block.timestamp`. `_updateTokenIndex` (L271-282) then persists both the unchanged index and the new timestamp. The elapsed window is consumed: the `_tokensAccrued` for that window are not pushed into the index, and because `timestamp` was overwritten, there is no way to recover them later — unlike a "pause" model where `timestamp` stays put and accrual resumes.

An attacker or any user can trigger the loss path permissionlessly via `updateBeforeMintOrBurn(token, account)` (L175, callable by anyone per the NatSpec "may be called by anyone to update stored indexes") or `updateBeforeTransfer`, or via `claimRewards` — each call updates the index/timestamp using whatever the token's `totalSupply()` is at that instant. The zero-supply window itself arises from ordinary user activity: a `DebtToken` whose debt is fully repaid (every borrow repaid → `totalSupply() == 0`), or a `DepositToken` fully withdrawn. Late depositors only start accruing from the *next* update after supply returns; `accountIndexOf` is set to the current global index in `_calculateTokenDelta` (L222-230), so they can never claim the skipped window — identical to the "lost forever" conclusion in the Blend report.

There is no owner/governor function to rescue `rewardToken` from the contract (`claimRewards` only pays out accrued amounts), so the skipped rewards are permanently locked.

### Impact Explanation
Freezing/loss of unclaimed yield: all reward tokens allocated to a token during periods when its `totalSupply() == 0` are neither distributed nor recoverable. If a rewarded DebtToken spends a long time at zero supply (common for an asset with no borrows), a large fraction of the emission budget is burned while users believe it is being distributed.

### Likelihood Explanation
Requires a rewarded token (`tokenSpeeds[token] > 0`, set by governor or `tokenSpeedKeeper` via `syncTokenSpeed`) to reach `totalSupply() == 0` for a nonzero duration. Zero `DebtToken` supply is normal — any asset with no outstanding borrows qualifies, and the deployer does not need to misconfigure anything; the bug is in the accounting invariant itself. Any index update during that window (permissionless `updateBeforeMintOrBurn`/`claimRewards`, or any mint/burn/transfer on other accounts) commits the loss. The immediate 0-supply case right when a DepositToken is added is also possible if speed is set before the first deposit.

### Recommendation
Do not advance `timestamp` while `totalSupply == 0`, so accrual effectively pauses and resumes when supply returns:

```solidity
if (_deltaTimestamps > 0 && _speed > 0) {
    uint256 _totalSupply = token_.totalSupply();
    if (_totalSupply > 0) {
        uint256 _tokensAccrued = _deltaTimestamps * _speed;
        uint256 _ratio = _tokensAccrued.wadDiv(_totalSupply);
        _newIndex = (_supplyState.index + _ratio).toUint224();
    }
    _newTimestamp = block.timestamp.toUint32(); // only if accrual happened, or keep timestamp unchanged on zero supply
}
```

Better: only set `_newTimestamp` when the index actually moved, so the un-accrued time remains owed to future suppliers. Alternatively, add a governor sweep function for undistributed `rewardToken` so skipped emissions are recoverable instead of permanently locked.

### Proof of Concept
Hardhat fork/unit PoC sketch against `contracts/RewardsDistributor.sol`:

```ts
// Setup: pool with DepositToken D and DebtToken T registered; governor sets
// rewardDistributor.updateTokenSpeed(T, speed = 1e18) while T.totalSupply() == 0
// (fresh deployment or all debt repaid).

// t0: speed activated -> tokenStates[T] = {index: INITIAL_INDEX, timestamp: t0}

// Warp 1 day with T.totalSupply() still 0, then any caller triggers an update:
await rewardDistributor.updateBeforeMintOrBurn(T.address, alice.address);
// _calculateTokenIndex: totalSupply == 0 -> _ratio = 0
// tokenStates[T] = {index: INITIAL_INDEX, timestamp: t0 + 86400}  // window consumed

// Now a user borrows, minting T (supply > 0). Warp another day.
// Alice claims:
await rewardDistributor.claimRewards(alice.address);
// Alice's index == INITIAL_INDEX == global index at her mint time;
// deltaIndex covers ONLY the second day. The 86400 * speed tokens from the
// zero-supply day are unclaimable by anyone and locked in the distributor.

const stuck = await rewardToken.balanceOf(rewardDistributor.address);
// assert(stuck >= 86400n * speed) — permanently unrecoverable
```

Key assertions: `tokenStates(T).timestamp` advances during zero supply while `index` stays flat, and after supply returns, no account can claim the skipped emissions (mirrors the `supply == 0 → index unchanged, time consumed` behavior of `update_emission_data`/`do_gulp_emissions` in the Blend report).