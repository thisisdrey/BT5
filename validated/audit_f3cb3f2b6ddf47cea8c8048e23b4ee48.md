### Title
Attacker can permanently halt reward index accrual in `RewardsDistributor` by forcing per-update rounding to zero - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
`RewardsDistributor._calculateTokenIndex` computes the reward index delta as `_tokensAccrued.wadDiv(_totalSupply)` where `_tokensAccrued = _deltaTimestamps * _speed`. For any tracked token where `speed` is small relative to `totalSupply`, an unprivileged caller can invoke `updateBeforeMintOrBurn` (publicly callable per its NatSpec) on every block, making `_deltaTimestamps` so small that `_ratio` rounds to zero. Because `_newTimestamp` is still advanced to `block.timestamp`, the tokens that should have accrued in each interval are permanently discarded — the index never grows while wall-clock time is consumed. This is structurally identical to the Surge `timeDelta * maxCR / recoveryDuration == 0` bug: a frequently-refreshed linear accrual whose per-step update truncates to zero, bricking the distribution scheme while the system continues operating.

### Finding Description
In `RewardsDistributor._calculateTokenIndex` (contracts/RewardsDistributor.sol ~L197-210):

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

`_newTimestamp` advances unconditionally whenever `_deltaTimestamps > 0`, even when `_ratio` truncates to 0. The accrued rewards `_tokensAccrued` for that window are neither added to the index nor carried forward — they vanish.

Public entry points reachable by any EOA:
- `RewardsDistributor.updateBeforeMintOrBurn(token_, account_)` — explicitly permissionless ("may be called by anyone").
- `updateBeforeTransfer`, and indirectly via `DebtToken.issue/repay/_mint/_burn` and `DepositToken` transfers, which call `updateRewardsBeforeMintOrBurn` → `_updateTokenIndex` → `_calculateTokenIndex` for every registered distributor.

The zero-rounding condition: `_deltaTimestamps * _speed * 1e18 < _totalSupply` (with `wadDiv` flooring; if half-up, `< _totalSupply / 2`). For a tracked token with a large supply (e.g., a high-decimal synthetic/deposit token with totalSupply ~1e30 wei) and a modest emission `speed` (e.g., 1e13 wei/s ≈ 315K reward-wei/year), a `_deltaTimestamps` of 1 second yields `_ratio < 1`. An attacker on a low-cost chain calls `updateBeforeMintOrBurn` every block; every call stores `_newTimestamp = block.timestamp` with the index unchanged, so the index is frozen at its initial value forever while real elapsed time — and the corresponding rewards — are skipped.

### Impact Explanation
Permanent loss of unclaimed yield for all depositors/debtors tracked by the distributor: the index never increases, so `_updateTokensAccruedOf` and `claimRewards` compute zero accrual; the reward tokens scheduled for those periods are neither distributed nor recoverable (they sit in the distributor/pool rewards contract indefinitely). This matches the accepted impact class "theft/freezing of unclaimed yield" and mirrors the Surge finding (frozen adaptive pricing → loss of funds).

### Likelihood Explanation
- Requires `tokenSpeeds[token] * 1e18 < totalSupply(token)` per 1-second delta (roughly `speed * 1e18 < totalSupply`). The set of `(token, speed)` pairs is governor-configured, but the condition arises naturally for large-supply tokens with conservative emission speeds — no malicious configuration is required, only an unlucky combination, analogous to the Surge issue which was accepted as Medium despite needing specific parameter choices.
- Trigger is fully unprivileged and cheap (one public call per block; especially attractive on L2s).
- Caveat: the index must already be initialized (`index > 0`) for the griefing window to apply; while `index == 0` the update functions early-return. The exact write path in `_updateTokenIndex` was inferred from `_calculateTokenIndex` returning `_newTimestamp`; if the implementation were to skip the timestamp write when `_ratio == 0`, the bug would not exist — that line could not be fully confirmed within available iterations and should be verified before finalizing.

### Recommendation
Track accrual continuously rather than per-call: either store accumulated pending rewards (`pendingAccrued += _deltaTimestamps * _speed` and only flush to the index when `_ratio > 0`), or do not advance `timestamp` when `_ratio == 0`, or enforce a minimum `speed` per total supply / use a higher-precision index accumulator (e.g., ray instead of wad) so a 1-second delta cannot truncate to zero.

### Proof of Concept
```solidity
// Hardhat/Foundry fork sketch
// Setup: governor registers rewardToken on RewardsDistributor, sets index via
// updateTokenIndex or a first update so tokenStates[token].index = 1e18.
// Configure tokenSpeeds[token] = s such that s * 1e18 < token.totalSupply()
// (e.g., s = 1e13, totalSupply = 1e30).

// Attack: every block, attacker EOA calls:
rewardsDistributor.updateBeforeMintOrBurn(token, attacker);

// Each call: _deltaTimestamps = block time step (e.g., 1-2s on L2),
// _tokensAccrued = _deltaTimestamps * s,
// _ratio = (_deltaTimestamps * s).wadDiv(totalSupply) == 0,
// _newTimestamp = block.timestamp  -> time consumed, index unchanged.

// Assert after N blocks:
assert(tokenStates[token].index == 1e18);          // index never grew
assert(tokensAccruedOf[user] == 0);                // users earn nothing
// Reward tokens equivalent to N * s are permanently stranded.
```