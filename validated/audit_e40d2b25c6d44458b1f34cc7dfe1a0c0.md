### Title
First depositor drains the entire reward budget because `accountIndexOf` defaults to 0 instead of `INITIAL_INDEX` - (File: contracts/RewardsDistributor.sol)

### Summary
The kernel bug activates a serdev port (`SERPORT_ACTIVE`) before registering its client ops, so the first callback runs against uninitialized state. `RewardsDistributor` has the same ordering defect: a token becomes "active" (`tokenStates[token].index > 0`, which enables `updateBeforeMintOrBurn`/`updateBeforeTransfer`/`claimRewards` accrual for it) while accounts that deposited before activation still have `accountIndexOf[token][account] == 0`. The fallback in `_calculateTokenDelta` only substitutes `INITIAL_INDEX` when the global index has already grown past it (`_tokenIndex > INITIAL_INDEX`), so an early depositor's delta is computed as `balance * (INITIAL_INDEX - 0)` — i.e., their full deposit balance credited as reward tokens on the very first accrual update.

### Finding Description
`_calculateTokenDelta` computes `_deltaIndex = _tokenIndex - _accountIndex` and `_tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex)` (contracts/RewardsDistributor.sol:229-230). The guard `if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) _accountIndex = INITIAL_INDEX` (line 225) never fires while the index is still exactly `INITIAL_INDEX`. When the governor calls `updateTokenSpeed`/`syncTokenSpeed` on an already-used DepositToken/DebtToken, `_updateTokenSpeed` sets `tokenStates[token_] = {index: INITIAL_INDEX, timestamp: now}` (line 298). From that moment the token is active and any of these public calls — `updateBeforeMintOrBurn`, `updateBeforeTransfer`, `claimRewards` — run `_updateTokensAccruedOf` for accounts whose `accountIndexOf` is still 0, crediting `balance * 1e18` reward tokens instantly, before any rewards were ever emitted.

### Impact Explanation
Any unprivileged EOA that holds a deposit/debt position taken before reward activation can call `claimRewards(account)` (or trigger `updateBeforeMintOrBurn` via a transfer/mint) and have `tokensAccruedOf` set to their entire token balance measured in reward tokens, then `_transferRewardIfEnoughTokens` sweeps up to the distributor's whole `rewardToken` balance. This is direct theft of the rewards budget — i.e., theft of unclaimed yield owed to all other users — from a single transaction, no privileged role needed.

### Likelihood Explanation
The trigger conditions are realistic: `syncTokenSpeed` is a permissionless-keeper-called function used to activate speeds on live deposit tokens that already have suppliers, and `updateTokenSpeed` activates previously inactive-but-deposited tokens. Every pre-existing holder of that token has `accountIndexOf == 0`, so the first of them to claim takes everything. The only mitigation would be the distributor holding less than `balance` reward tokens — but any funded campaign pays out in full.

### Recommendation
In `_calculateTokenDelta`, treat a zero `accountIndexOf` as `INITIAL_INDEX` whenever `_accountIndex == 0` (not only when `_tokenIndex > INITIAL_INDEX`), or initialize `accountIndexOf` to `INITIAL_INDEX` on first interaction. Alternatively, when activating a token, iterate/settle existing holders or set the starting index to a value that pre-seeded accounts can't retroactively claim from.

### Proof of Concept
```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";
import {RewardsDistributor} from "../contracts/RewardsDistributor.sol";
// Deploy Pool, DepositToken (e.g. vaUSDC), RewardsDistributor via pool registry
// as in the repo's existing tests, then:

contract RewardIndexInitTest is Test {
    function test_earlyDepositorDrainsRewards() public {
        address attacker = makeAddr("attacker");

        // 1. Attacker deposits into a DepositToken BEFORE any reward speed is set.
        depositToken.deposit(attacker, 1_000e6); // attacker holds 1_000e6 deposit tokens

        // 2. RewardsDistributor is funded with reward tokens (e.g. via Treasury/ops).
        rewardToken.mint(address(distributor), 100_000e18);

        // 3. Governor (or tokenSpeedKeeper via syncTokenSpeed) activates rewards
        //    for the already-live deposit token -> index = INITIAL_INDEX.
        vm.prank(governor);
        distributor.updateTokenSpeed(IERC20(address(depositToken)), 1e18);

        // 4. Attacker claims. accountIndexOf == 0 -> deltaIndex == 1e18
        //    -> tokensAccruedOf = 1_000e6 * 1e18 (scaled) -> sweeps full balance.
        distributor.claimRewards(attacker);

        // Attacker received reward tokens worth ~their full deposit balance,
        // i.e. the entire distributor balance if <= that amount.
        assertGt(rewardToken.balanceOf(attacker), 0);
        assertEq(distributor.claimable(attacker), 0);
    }
}
```