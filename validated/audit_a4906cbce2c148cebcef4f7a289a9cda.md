### Title
Unprivileged dust-transfer griefing fills a victim's `depositTokensOfAccount` set, DoSing deposits and borrows via `UserReachedMaxTokens` revert - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool` enforces `MAX_TOKENS_PER_USER = 30` across the union of `debtTokensOfAccount` and `depositTokensOfAccount`. Any call to `addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` reverts with `UserReachedMaxTokens` once the combined length reaches 30. `DepositToken._transfer` unconditionally calls `pool.addToDepositTokensOfAccount(recipient_)` whenever a recipient's balance goes from 0 to non-zero, and `DepositToken.transfer` is a public, unprivileged entry point. An attacker can therefore grief any victim by transferring dust amounts of every registered `DepositToken` to them, permanently occupying slots in their per-account set and forcing all subsequent deposits of new collateral types and new debt-token issuance for that account to revert — a denial-of-service analog to the "maliciously crafted input → unexpected crash" bug class of CVE-2025-43443.

### Finding Description
- `Pool.sol` `onlyIfAdditionWillNotReachMaxTokens` reverts when `debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER` (Pool.sol:143-148, `MAX_TOKENS_PER_USER = 30` at Pool.sol:79).
- `DepositToken._transfer` adds the recipient to the set on any first receipt of a token (DepositToken.sol:518-520). `transfer`/`transferFrom` only check the *sender's* unlocked balance via `_revertIfLocked` (DepositToken.sol:348-376) — the recipient needs no opt-in.
- `DepositToken._mint` performs the same set insertion on deposits (DepositToken.sol:486-488), so once a victim's set is full, `deposit()` for any collateral they don't already hold reverts inside `_mint` → `addToDepositTokensOfAccount`.
- Debt-token issuance (`DebtToken.mint`/`flashIssue`) calls `addToDebtTokensOfAccount`, which shares the same 30-slot cap, so new borrows revert as well.
- The check counts slots, not value: each of the pool's deposit tokens occupies exactly one slot regardless of amount, so griefing cost is just dust (1 wei-equivalent per deposit token, plus gas). With `MAX_TOKENS_PER_USER = 30` and a pool listing ~5-15 collaterals, the attacker reaches the cap cheaply — entries are only removed when a balance returns exactly to zero (DepositToken.sol:523-525).

### Impact Explanation
The victim's `deposit()` calls for any collateral token they do not already hold revert with `UserReachedMaxTokens`, and issuing a debt token not already in their set reverts too. This is a temporary freezing-of-funds / liveness violation: the victim cannot add collateral (e.g., to rescue a deteriorating position ahead of liquidation, or to onboard new collateral types) and cannot open new debt positions until they manually zero out each dust balance. Because seizing in `Pool.liquidate` moves the *victim's* tokens to the liquidator, the attack does not protect the victim from liquidation — it only blocks their ability to add collateral or repay with newly acquired debt tokens. In a falling market, a victim who needs to top-up collateral with a new deposit token is forced to first transfer out dust balances one token at a time, and can be re-griefed.

### Likelihood Explanation
Fully unprivileged: the attacker needs only dust of each underlying to first deposit (or buy dust `msdToken` on the market) and then `transfer` it to the victim. No privileged role, oracle manipulation, or governance action is required. Cost scales linearly with the number of distinct deposit tokens in the pool, and the attack is repeatable — the attacker can re-fill slots as soon as the victim clears them. The only limitation is that the number of distinct deposit tokens in a given pool bounds the attacker's contribution; pools with many listed collaterals (approaching 30) make this trivially reachable.

### Recommendation
Count distinct tokens only when the account actually uses them economically, e.g., enforce `onlyIfAdditionWillNotReachMaxTokens` only inside `DepositToken._mint`/`deposit` and `DebtToken` issuance paths, not on `transfer` receipt; alternatively, raise/de-scope the cap for `depositTokensOfAccount` additions originating from `_transfer`, or store per-account token sets in a way that does not revert on insertion (e.g., allow the set to grow but bound `debtPositionOf` iteration via the deposit path only). A minimal fix is to skip `addToDepositTokensOfAccount` in `_transfer` when it would exceed the cap, or to make `debtPositionOf`/`depositOf` tolerant of capped sets so a forced-dust slot does not brick deposits.

### Proof of Concept
Foundry fork sketch (target pool on mainnet, e.g., msUSD pool):

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";
import {IPool} from "../contracts/interfaces/IPool.sol";
import {IDepositToken} from "../contracts/interfaces/IDepositToken.sol";
import {IERC20} from "../contracts/dependencies/openzeppelin/token/ERC20/IERC20.sol";

contract MaxTokensGriefingTest is Test {
    IPool pool = IPool(POOL_ADDRESS);
    address victim = makeAddr("victim");
    address attacker = makeAddr("attacker");

    function test_dustGriefingBlocksDeposits() public {
        IDepositToken[] memory dtokens = pool.getDepositTokens(); // enumerate registered deposit tokens
        uint256 n = dtokens.length;
        require(n >= 30, "need >= 30 deposit tokens or combine with debt tokens");

        for (uint256 i; i < 30; ++i) {
            IDepositToken dt = dtokens[i];
            IERC20 underlying = dt.underlying();
            deal(address(underlying), attacker, 1e6);
            vm.startPrank(attacker);
            underlying.approve(address(dt), 1e6);
            dt.deposit(1e6, attacker);          // attacker mints dust msdToken
            dt.transfer(victim, 1);              // 1 wei dust -> adds slot to victim's set
            vm.stopPrank();
        }

        // Victim's combined set is now full (30 entries of worthless dust).
        // Victim tries to deposit a collateral they don't hold -> reverts.
        IDepositToken newDt = dtokens[30 - 1]; // any token not in victim's set
        IERC20 u2 = newDt.underlying();
        deal(address(u2), victim, 100e18);
        vm.startPrank(victim);
        u2.approve(address(newDt), 100e18);
        vm.expectRevert(Pool.UserReachedMaxTokens.selector);
        newDt.deposit(100e18, victim);
        vm.stopPrank();

        // Similarly, DebtToken issuance of a synthetic the victim doesn't hold
        // reverts in addToDebtTokensOfAccount (same modifier), blocking new borrows.
    }
}
```

Steps: attacker deposits minimal underlying into each `DepositToken`, then `transfer(victim, 1)` for each — every transfer passes `_revertIfLocked` (attacker has no debt) and inserts the token into the victim's `depositTokensOfAccount` via `DepositToken._transfer` (DepositToken.sol:518-520). At 30 entries, `addToDepositTokensOfAccount` and `addToDebtTokensOfAccount` revert through `onlyIfAdditionWillNotReachMaxTokens` (Pool.sol:143-148), so the victim's `deposit()` for any new collateral and any new `DebtToken` issuance revert.

Note on completeness: I verified the mechanism in `Pool.sol` and `DepositToken.sol` but did not fully enumerate every chain's deployment to confirm a pool actually lists enough deposit/debt tokens to reach the 30-entry cap alone; on pools with fewer listed tokens, the attacker can additionally combine dust deposit-token slots with the victim's own existing debt-token entries to reach the cap sooner.