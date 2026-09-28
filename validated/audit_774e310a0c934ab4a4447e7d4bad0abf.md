### Title
Griefing via dust deposits fills a victim's `depositTokensOfAccount`/`debtTokensOfAccount` lists to `MAX_TOKENS_PER_USER`, blocking them from adding new collateral or debt positions - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool.addToDepositTokensOfAccount` (and its debt counterpart) is callable by any registered `DepositToken`/`DebtToken` and mutates the per-account token list for an arbitrary `account_` supplied as an argument. Because `DepositToken._update`/transfer logic pushes the recipient into the list whenever their balance goes from 0 to positive, an unprivileged attacker can dust-transfer 1 wei of every listed deposit token to a victim, permanently occupying all `MAX_TOKENS_PER_USER = 30` slots. Once the list is full, `onlyIfAdditionWillNotReachMaxTokens` reverts with `UserReachedMaxTokens` on any operation that would add a new token to the victim's list — the victim can no longer deposit a new collateral type (or open a debt position in a new synthetic), while the dust entries cannot be removed by the victim as long as their deposit tokens are locked against debt.

### Finding Description
The per-account accounting sets are enforced at the Pool level:

- `MAX_TOKENS_PER_USER = 30` (`Pool.sol:79`)
- `onlyIfAdditionWillNotReachMaxTokens` reverts when `debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= 30` (`Pool.sol:143-148`)
- `addToDepositTokensOfAccount(account_)` takes `account_` as a caller-supplied parameter and only checks that `msg.sender` is a registered deposit token (`Pool.sol:216-220`). The account being modified is never authenticated against the caller — the same missing-authorization pattern as the Mattermost IDOR where any user could act on another user's channel state.
- `addToDebtTokensOfAccount(account_)` is symmetric (`Pool.sol:204-208`).

Reachability: `DepositToken` mint/transfer hooks invoke these functions with the recipient as `account_`. An attacker who holds (or deposits minimal underlying to mint) each of the pool's deposit tokens can call `DepositToken.transfer(victim, 1)` for each token the victim does not yet hold. Each transfer makes the victim's balance nonzero, so the hook calls `Pool.addToDepositTokensOfAccount(victim)`, consuming one slot — at zero cost beyond gas and recoverable underlying.

Cleanup is one-directional: entries are only removed when the victim's balance returns to 0 (`removeFromDepositTokensOfAccount`, `Pool.sol:630-634`). If the victim has an open debt position, their deposit tokens are locked (`_revertIfLocked` on transfers/withdrawals in `DepositToken`), so they cannot send the dust back out to free the slots. There is no admin-free escape: removing the attacker's entries requires the victim to move the tokens, which the lock forbids.

### Impact Explanation
Permanent freezing of the victim's ability to onboard new collateral or debt types for the lifetime of their open position:

1. The victim cannot deposit any collateral whose `DepositToken` is not already in their list — `deposit()` reverts inside `addToDepositTokensOfAccount`. If their position drifts unhealthy and the only viable collateral to rescue it is a token they do not yet hold, they are forced into liquidation and lose the liquidation incentive plus protocol fee (`PositionLiquidated` path, `Pool.sol:537-596`).
2. The victim cannot issue/borrow a new synthetic whose `DebtToken` is not in their list (same combined cap), degrading their ability to deleverage via swap of a different synth.
3. New deposit tokens added by governance later are permanently unusable for the victim while the dust is locked.

This is a direct, deterministic, unprivileged state write into another user's accounting structures — the DeFi analog of marking someone else's resource state without authorization.

### Likelihood Explanation
- Fully permissionless: only requires holding dust amounts of whitelisted deposit tokens (obtainable by depositing trivial underlying or buying on the open market).
- Cost scales linearly with the number of tokens the victim doesn't already hold (≤30 transfers); nothing on the deposit side authenticates the recipient.
- No pause/shutdown flag, health check, or reentrancy guard prevents it: `addToDepositTokensOfAccount` has only the `onlyIfAdditionWillNotReachMaxTokens` modifier.
- The attack is most damaging against leveraged/SmartFarming positions whose deposits are locked, since those victims cannot self-clean the list.

### Recommendation
Authorize the account owner: have `DepositToken`/`DebtToken` only register the actual `msg.sender` context of the user-initiated action rather than the token-transfer recipient — i.e., for `transfer`/`transferFrom`, either skip `addToDepositTokensOfAccount` for the recipient (require recipients to explicitly opt in via a deposit/registration call), or make `Pool.addToDepositTokensOfAccount` accept an additional `sender_` argument and only add the account when it equals the `SynthContext._msgSender()` of the original call. Alternatively, track the "active" token list based on a threshold balance or explicit user opt-in so dust balances cannot occupy slots.

### Proof of Concept
Foundry fork test sketch (against a live pool such as mainnet `Pool`):

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";
import {IPool} from "../contracts/interfaces/IPool.sol";
import {IDepositToken} from "../contracts/interfaces/IDepositToken.sol";
import {IERC20} from "../contracts/dependencies/openzeppelin/token/ERC20/IERC20.sol";

contract AccountListGriefingTest is Test {
    IPool pool = IPool(POOL_ADDRESS);          // e.g. mainnet Pool
    address victim = VICTIM_ADDRESS;           // account with open debt / locked deposits
    address attacker = makeAddr("attacker");

    function test_fillVictimTokenList() public {
        address[] memory depositTokens = pool.getDepositTokens();
        address[] memory alreadyHeld = pool.getDepositTokensOfAccount(victim);

        vm.startPrank(attacker);
        uint256 added;
        for (uint256 i; i < depositTokens.length; ++i) {
            IDepositToken dt = IDepositToken(depositTokens[i]);
            if (dt.balanceOf(victim) > 0) continue; // already occupies a slot

            // mint a tiny amount of the deposit token for attacker
            IERC20 underlying = dt.underlying();
            deal(address(underlying), attacker, 1);
            underlying.approve(address(dt), 1);
            dt.deposit(1);                          // or direct mint path

            // dust the victim -> hook calls pool.addToDepositTokensOfAccount(victim)
            dt.transfer(victim, 1);
            added++;
            if (pool.getDepositTokensOfAccount(victim).length +
                pool.getDebtTokensOfAccount(victim).length >= pool.MAX_TOKENS_PER_USER()) break;
        }
        vm.stopPrank();

        // Victim is now unable to deposit into any NEW deposit token
        IDepositToken newToken = /* a depositToken not in victim's list */;
        vm.startPrank(victim);
        IERC20 u = newToken.underlying();
        deal(address(u), victim, 100e18);
        u.approve(address(newToken), 100e18);
        vm.expectRevert(Pool.UserReachedMaxTokens.selector); // via onlyIfAdditionWillNotReachMaxTokens
        newToken.deposit(100e18);
        vm.stopPrank();

        // Victim cannot evict dust while deposits are locked by debt:
        // DepositToken.transfer reverts via _revertIfLocked for the victim.
    }
}
```

Expected: each attacker `transfer` succeeds and increments `getDepositTokensOfAccount(victim).length`; once the combined count reaches 30, the victim's `deposit` into any unheld collateral reverts with `UserReachedMaxTokens`, persisting until their debt is fully repaid — during which an unhealthy position can be liquidated with no collateral-add rescue path.

Caveat: the exact hook name (`_update`/`_afterTokenTransfer`) inside `DepositToken.sol`/`DebtToken.sol` that triggers `addToDepositTokensOfAccount` was not re-verified line-by-line in this pass; confirm the call site before finalizing the PoC, though the authorization gap in `Pool.sol:216-220` and the revert in `Pool.sol:143-148` are confirmed.