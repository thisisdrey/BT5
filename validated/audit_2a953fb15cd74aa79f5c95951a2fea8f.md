### Title
Reward tokens deposited to `RewardsDistributor` become permanently locked when token speed is zero or the distributor is removed — no recovery mechanism exists - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
`RewardsDistributor` holds the `rewardToken` balance that backs all user reward claims. Unlike the rest of the Metronome protocol contracts, it does **not** inherit `TokenHolder`/`sweep`, and it exposes no function to withdraw surplus or stranded reward tokens. Tokens can only leave the contract through `_transferRewardIfEnoughTokens` inside `claimRewards`, which pays strictly per-account accrued amounts. If the distributor is funded while `tokenSpeeds[token_] == 0`, after speeds are set to zero, or after the distributor is removed from the pool, the deposited rewards accrue to nobody and are locked in the contract forever — the same "incentive tokens permanently locked in zero-activity periods" flaw as the Debita report.

### Finding Description
The contract's only outflow path is the private payout helper:

```solidity
// contracts/RewardsDistributor.sol:248-256
function _transferRewardIfEnoughTokens(address account_, uint256 amount_) private {
    IERC20 _rewardToken = rewardToken;
    uint256 _balance = _rewardToken.balanceOf(address(this));
    if (amount_ > 0 && amount_ <= _balance) {
        tokensAccruedOf[account_] = 0;
        _rewardToken.safeTransfer(account_, amount_);
        emit RewardClaimed(account_, amount_);
    }
}
```

Accrual is driven purely by `tokenSpeeds` and elapsed time in `_calculateTokenIndex` (lines 197-212). Rewards are funded by direct ERC20 transfers to the contract (there is no deposit function and no accounting of "funded vs distributed"). The inheritance chain is `Initializable`, `ReentrancyGuardDeprecated`, `ReentrancyGuardTransient`, `Manageable`, `RewardsDistributorStorageV2` — `Manageable` extends `SynthContext`, not `Governable`, so the `sweep()` recovery that `TokenHolder` provides to `Pool`, `DepositToken`, `Treasury`, `NativeTokenGateway`, `VesperGateway`, and `RecurringAirdrop` is absent here. There is no `withdraw`, `sweep`, `recover`, or refund-to-funder path anywhere in the file.

Analog to Debita `incentivizePair`:
- Depositor (governor/funder, or the Vesper reward stream mirrored via `syncTokenSpeed`) sends reward tokens to the contract expecting them to be distributed over future periods.
- If a period passes with `tokenSpeeds[token_] == 0` (speed never set, set to 0, or token removed from `tokens`), no index accrues and the tokens backing that period are stranded.
- If the distributor is de-registered via `Pool.removeRewardsDistributor`, `onlyIfDistributorExists` blocks further `updateTokenSpeed`, but users can still only claim what they already accrued — any un-accrued surplus balance has no exit.

### Impact Explanation
Permanent freezing of funds: any `rewardToken` balance in excess of the cumulative accrued `tokensAccruedOf` is locked in the contract forever. This includes over-funded rewards, rewards funded for periods with zero speed (the "zero-activity epoch" analog), and rewards remaining after the distributor is removed from the pool. Unlike `Treasury.claimFromVesper` (which computes and forwards only the surplus balance) or `TokenHolder.sweep`, there is no mechanism to recover or redirect unclaimed incentives — identical impact to the referenced Debita finding.

### Likelihood Explanation
No attacker action is required; this is a reachable state under normal operation. Funding the distributor with reward tokens ahead of distribution, setting a speed to zero mid-program (governor may do this to pause emissions), changing reward schedules, or replacing the distributor all leave stranded balances. `syncTokenSpeed` derives speed from an external Vesper `poolRewards` rate — if the external program ends (periodFinish passed), speed becomes 0 while already-transferred tokens remain in the contract with no exit.

### Recommendation
Make `RewardsDistributor` inherit `TokenHolder` (via `Governable`-style `_requireCanSweep` restricted to the pool governor) or add an explicit `recoverRewardToken(to_, amount_)` governor function that only permits withdrawing the surplus above total unclaimed accruals. Alternatively, mirror `Treasury.claimFromVesper`'s surplus-only accounting so only the undistributed excess can be swept.

### Proof of Concept
Hardhat fork sketch:

```solidity
// RewardsDistributor rd is registered on Pool; rewardToken = e.g. MET/opMET
// 1. Governor funds the distributor but never sets a speed (or sets speed, then 0):
rewardToken.transfer(address(rd), 1_000e18);

// 2. Time passes with tokenSpeeds[depositToken] == 0 -> _calculateTokenIndex accrues 0.
vm.warp(block.timestamp + 30 days);

// 3. No user has tokensAccruedOf > 0. rd.claimRewards(user, tokens) transfers nothing.
// 4. There is no sweep/withdraw on rd (it does not inherit TokenHolder).
//    Assert: rewardToken.balanceOf(address(rd)) == 1_000e18 and no callable
//    function can move it -> permanently locked.
```

Verified against `contracts/RewardsDistributor.sol`: the only `safeTransfer` of `rewardToken` is inside `_transferRewardIfEnoughTokens` gated on `tokensAccruedOf`, and the contract's inheritance list contains no `TokenHolder`/`sweep`.

Note: I confirmed `RecurringAirdrop`/`MetAirdrop` are *not* affected (they inherit `Governable` → `TokenHolder.sweep`). One caveat: I did not enumerate `RewardsDistributorStorageV2`/V1 storage files for an additional recovery function; the contract source itself contains none, and the deployed ABI listings show no `sweep` on the distributor.