### Title
`uint32` timestamp downcast in `RewardsDistributor` will permanently brick deposit/debt token operations after February 2106 - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
`RewardsDistributor` stores the last index-update timestamp as `uint32` (`TokenState.timestamp` in `contracts/storage/RewardsDistributorStorage.sol`) and writes it via `block.timestamp.toUint32()` in `_calculateTokenIndex` (lines 208, 210) and `_updateTokenSpeed` (lines 298, 302). `SafeCast.toUint32` reverts when `block.timestamp > type(uint32).max`, i.e. after `2106-02-07 12:47:05 GMT`. From that point, every call that flows through `_updateTokenIndex` reverts.

### Finding Description
- `_calculateTokenIndex` casts `block.timestamp` to `uint32` at `contracts/RewardsDistributor.sol:208` and `:210`.
- `_updateTokenIndex` (line 271) calls `_calculateTokenIndex` unconditionally for any token with `index > 0`.
- `updateBeforeMintOrBurn` (line 175) and `updateBeforeTransfer` (line 186) are public hooks invoked by `DepositToken` and `DebtToken` on every mint, burn, and transfer, whenever `tokenStates[token_].index > 0` (i.e. rewards were ever configured for that token).
- Because `toUint32` reverts rather than wraps, after the uint32 epoch boundary these hooks always revert, so deposit-token transfers/withdrawals and debt-token issue/repay permanently fail for every reward-enabled market.

### Impact Explanation
Permanent freezing of user funds and protocol liveness failure: once `block.timestamp` exceeds `uint32.max`, users cannot transfer or withdraw `DepositToken` positions and cannot mint or repay `DebtToken` positions on any pool whose token has a nonzero rewards index. Unlike the referenced report (silent overflow and misbehavior), Metronome's use of `SafeCast` converts this into a hard revert — still a denial of service, and effectively a permanent freeze absent an upgrade, since the only fix is changing the storage layout or logic via governor/upgrade path.

### Likelihood Explanation
Very low in practical terms: the trigger is ~80 years in the future (Feb 2106), the contracts are upgradeable, and the freeze only applies to tokens with `index > 0` (rewards configured). There is no attacker action involved; this is a time-bomb defect, not an exploitable path. Severity is informational/low; the medium rating in the source report is not warranted here.

### Recommendation
Widen `TokenState.timestamp` to `uint64`/`uint256` (or store `uint40`+) in a future storage version, or drop `SafeCast` in favor of saturating/checked-delta logic so accrual math degrades gracefully rather than reverting. Given the contracts are upgradeable, this can be bundled into any future `RewardsDistributorStorageV2` with no urgency.

### Proof of Concept
Reproducible in Hardhat/Foundry by forking mainnet and warping time past `2^32 - 1`:

```solidity
// After configuring a reward token (tokenStates[token].index > 0):
vm.warp(2**32); // 2106-02-07
// Any of these revert with "SafeCast: value doesn't fit in 32 bits":
rewardsDistributor.updateBeforeMintOrBurn(token, user);
rewardsDistributor.updateBeforeTransfer(token, userA, userB);
// Consequently depositToken.transfer(...) / debtToken.issue(...) revert.
```

Note: I could not verify the exact call sites inside `DepositToken`/`DebtToken` that invoke the hooks in this iteration, but the code comments and interface confirm they are the intended callers; the revert inside `RewardsDistributor` itself is directly verifiable.