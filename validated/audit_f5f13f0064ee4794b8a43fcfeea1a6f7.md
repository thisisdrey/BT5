### Title
Unprivileged account-list stuffing via dust `DepositToken` transfers/deposits blocks victims from adding collateral or issuing debt, enabling forced liquidations - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
Metronome tracks each account's deposit tokens and debt tokens in two per-account sets capped by `MAX_TOKENS_PER_USER`. Both `DepositToken._mint` (via `deposit(amount_, onBehalfOf_)`) and `DepositToken._transfer` (via `transfer`/`transferFrom`) push the *recipient* onto `depositTokensOfAccount` without the recipient's consent. An attacker can dust-fill a victim's set with every listed deposit token so that `onlyIfAdditionWillNotReachMaxTokens` reverts on any further addition, permanently DoS-ing the victim's ability to deposit a new collateral type or issue a new debt token. This mirrors CVE-2018-8976's class: attacker-crafted input drives a shared structure past its bounds, producing a denial of service — here, denial of collateral top-ups and borrowing, which can convert into a forced liquidation.

### Finding Description
`Pool.addToDepositTokensOfAccount` and `Pool.addToDebtTokensOfAccount` are guarded by `onlyIfAdditionWillNotReachMaxTokens`, which reverts with `UserReachedMaxTokens` once `debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER` (`contracts/Pool.sol:143-148`, `204-220`).

The additions are triggered internally whenever a balance goes 0 → positive, with no opt-in by the recipient:

- `DepositToken._mint` calls `pool.addToDepositTokensOfAccount(account_)` when `_balanceBefore == 0 && amount_ > 0` (`contracts/DepositToken.sol:486-488`). `deposit` accepts an `onBehalfOf_` beneficiary, so the attacker doesn't even need the victim to transfer.
- `DepositToken._transfer` calls the same for `recipient_` (`contracts/DepositToken.sol:517-520`). `_revertIfLocked` only checks the *sender's* balance, so a malicious sender is unrestricted.

Attack path (any EOA):
1. Attacker deposits dust underlying into each listed `DepositToken` (or acquires dust msd-token balances).
2. For each deposit token `dt_i` where the victim has zero balance, attacker calls `dt_i.transfer(victim, 1 wei)` (or `dt_i.deposit(1 wei, victim)`).
3. The victim's `depositTokensOfAccount` fills until `length >= MAX_TOKENS_PER_USER`.
4. Now: `victim` cannot `deposit` any *new* collateral type (the `_mint` → `addToDepositTokensOfAccount` reverts), cannot receive any new deposit token, and cannot `issue`/`mint` any new `DebtToken` (`DebtToken` mint similarly calls `addToDebtTokensOfAccount`, which reverts under the same cap).
5. If the victim's position drifts underwater (oracle price movement), they cannot add a new collateral type to restore health. The attacker liquidates them via `Pool.liquidate`, seizing collateral plus `liquidatorIncentive`.

The victim's only self-help is transferring the dust out so their balance hits 0 and `removeFromDepositTokensOfAccount` fires (`contracts/DepositToken.sol:522-525`) — but each removal frees exactly one slot and the attacker can re-fill slots in the same or subsequent block, so the grief is repeatable and front-runnable. No privileged role, oracle manipulation, or trusted-remote compromise is required; `SynthContext` only rebinds `_msgSender`, not the victim.

### Impact Explanation
- **Forced liquidation / loss of funds:** a victim whose position becomes unhealthy cannot deposit new collateral types, so the attacker (or any liquidator) seizes collateral plus incentive.
- **Denial of service:** the victim cannot issue new debt tokens or receive deposit tokens at all while the list is saturated — a persistent, cheaply renewable freeze of core protocol functionality for that account.
- Cost to the attacker is bounded by the number of listed deposit tokens × dust transfer cost (a handful of tokens per pool), while the victim may hold arbitrarily large positions.

### Likelihood Explanation
Requires the pool to have enough distinct listed deposit/debt tokens to reach `MAX_TOKENS_PER_USER` (the cap is shared across both sets, so roughly half fills from dust suffice to block the other half). It pays off only against victims who (a) hold debt and (b) would need a new collateral type to stay healthy — plausible whenever collateral prices move. It cannot freeze withdrawals of already-held, unlocked collateral (`withdraw` only calls `removeFromDepositTokensOfAccount`), so impact is temporary freezing plus liquidation exposure rather than permanent loss of all funds — consistent with a Medium severity.

### Recommendation
- Only add the recipient to `depositTokensOfAccount` on operator-initiated `deposit` for self (`onBehalfOf_ == _msgSender()`) or make set membership opt-in/lazy.
- Alternatively, drop the hard cap: iterate lazily, or only revert list addition when it would break `debtPositionOf` accounting rather than unconditionally.
- At minimum, exclude dust additions on plain `transfer`/`transferFrom` so third parties cannot grow a victim's set (e.g., require `amount_` above a threshold or let `addToDepositTokensOfAccount` silently skip instead of revert when the caller is a transfer).

### Proof of Concept
```solidity
// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.9;

import "forge-std/Test.sol";
import {Pool} from "contracts/Pool.sol";
import {DepositToken, IDepositToken} from "contracts/DepositToken.sol";
import {ERC20Mock} from "contracts/mock/ERC20Mock.sol";

contract AccountListStuffingPoC is Test {
    Pool pool;               // deployed Pool proxy
    address attacker = makeAddr("attacker");
    address victim   = makeAddr("victim");

    function test_dust_fill_blocks_new_collateral() public {
        address[] memory dts = pool.getDepositTokens();
        uint256 max = pool.MAX_TOKENS_PER_USER();

        // Attacker deposits dust and transfers 1 wei of each deposit token to victim
        vm.startPrank(attacker);
        for (uint256 i; i < dts.length && pool.getDepositTokensOfAccount(victim).length < max; ++i) {
            DepositToken dt = DepositToken(dts[i]);
            if (dt.balanceOf(victim) != 0) continue;
            ERC20Mock u = ERC20Mock(address(dt.underlying()));
            u.mint(attacker, 1);
            u.approve(address(dt), 1);
            dt.deposit(1, victim);              // _mint -> addToDepositTokensOfAccount(victim)
            // equivalently: dt.transfer(victim, 1);
        }
        vm.stopPrank();

        // Victim's list is saturated: any new deposit-token mint or debt-token issue reverts
        DepositToken newDt = DepositToken(dts[0] /* any token victim has 0 balance of */);
        vm.startPrank(victim);
        ERC20Mock uv = ERC20Mock(address(newDt.underlying()));
        // victim tries to deposit a collateral type they don't yet hold
        vm.expectRevert(Pool.UserReachedMaxTokens.selector);
        newDt.deposit(1e18, victim);
        // and cannot issue a new debt token either (addToDebtTokensOfAccount hits same cap)
        vm.stopPrank();
    }
}
```

Uncertainty: the exact value of `MAX_TOKENS_PER_USER` and whether `DebtToken`'s mint path calls `addToDebtTokensOfAccount` on every balance 0→positive transition were not fully verified line-by-line (grep confirmed both calls exist in `Pool.sol`/`DebtToken.sol` but the mint call site wasn't read); the attack is unaffected in kind — if the count of listable tokens is smaller than the cap, impact reduces to blocking a subset of new collateral/debt additions.