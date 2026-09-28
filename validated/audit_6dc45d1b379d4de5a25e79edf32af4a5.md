### Title
Unprivileged dust transfers can fill a victim's per-account token list (`depositTokensOfAccount`/`debtTokensOfAccount`) up to `MAX_TOKENS_PER_USER`, permanently blocking the victim from depositing new collateral types or opening new debt positions - ([File: contracts/Pool.sol](contracts/Pool.sol), [File: contracts/DepositToken.sol](contracts/DepositToken.sol))

### Summary
Analogous to the HatsSignerGate bug — where an externally-maintained module list diverges from the gate's own accounting and breaks core functionality — `Pool` maintains per-account enumerable sets (`depositTokensOfAccount`, `debtTokensOfAccount`) that are updated as a side effect of token balance changes. Because `DepositToken._transfer` adds the token to the *recipient's* list on any 0→nonzero balance change, any unprivileged account can force-insert entries into a victim's list by dust-transferring deposit tokens. When the list reaches `MAX_TOKENS_PER_USER`, the `onlyIfAdditionWillNotReachMaxTokens` check in `Pool.addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` reverts, so any subsequent `_mint`/`_transfer` of a *new* token type to the victim reverts — blocking deposits, transfers, liquidation `seize` proceeds, and leverage flows for that account.

### Finding Description
`Pool` tracks each account's deposit/debt tokens in `MappedEnumerableSet` structures. Entries are added inside `DepositToken._mint`/`_transfer` and `DebtToken` equivalents whenever the balance moves 0→nonzero, via `Pool.addToDepositTokensOfAccount(account_)` / `addToDebtTokensOfAccount(account_)`. Both are gated by `onlyIfAdditionWillNotReachMaxTokens(account_)` (`Pool.sol:204-220`).

Critically, `DepositToken.transfer`/`transferFrom` are public and only check the *sender's* unlocked balance (`_revertIfLocked(sender_, amount_)`) — the recipient cannot opt out (`DepositToken.sol:348-376`). `_transfer` unconditionally calls `pool.addToDepositTokensOfAccount(recipient_)` on first receipt (`DepositToken.sol:517-525`).

Attack path (fully unprivileged):
1. Attacker deposits (or obtains) a dust amount of every listed deposit token across the pool's collaterals.
2. Attacker calls `depositToken.transfer(victim, 1)` for each token until `depositTokensOfAccount.length(victim) == MAX_TOKENS_PER_USER`.
3. Victim's `depositTokenX.deposit(amount, victim)` for any collateral not already in their list now reverts in `_mint` → `addToDepositTokensOfAccount` → `MaxNumberOfTokens`. Same for receiving deposit tokens via `transfer`, `seize` during liquidation, or SmartFarmingManager leverage callbacks.
4. The symmetric `debtTokensOfAccount` path blocks the victim from minting new synth debt types (each `DebtToken` mint calls `addToDebtTokensOfAccount`).

The same divergence class exists in the debt direction: an attacker-controlled liquidation flow or flash-issue path that creates a first-time debt balance for a third party also consumes a `debtTokensOfAccount` slot without consent.

### Impact Explanation
Liveness / freezing of functionality: the victim cannot deposit collateral of any new type, cannot receive deposit tokens (including as a liquidation beneficiary), and cannot open debt in new synths. If the victim holds a position needing a *new* collateral type to restore health, they can be forced into liquidation. Recovering requires the victim to zero out dust balances (each transfer-out removes a slot via `removeFromDepositTokensOfAccount`), but the attacker can re-grief in the same block as the victim's deposit, so the DoS is persistent while the attacker is willing to spend gas. This is a temporary freezing of funds / broken core functionality, matching the reported bug class (external party corrupting a count/list the protocol relies on).

### Likelihood Explanation
- Attacker needs only dust amounts of each listed deposit token (obtainable by depositing minimum amounts or buying/receiving them); no privileged role, oracle manipulation, or governance action required.
- `transfer`/`transferFrom`/`seize` all funnel through `_transfer` which performs the insertion; there is no recipient-consent or whitelist.
- Likelihood is bounded by `MAX_TOKENS_PER_USER` (gas cost scales with the cap and number of listed collaterals) and by the victim's ability to clear slots — but since griefing only needs to front-run the victim's deposit of a *new* collateral type, it is cheap to sustain.

### Recommendation
- Do not gate `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` reversion on transfers initiated by third parties; instead, either allow the list to exceed the cap for forced entries, or only enforce `onlyIfAdditionWillNotReachMaxTokens` on the account's own deposit/mint actions (check `_msgSender() == account_` or pass a flag).
- Alternatively, compute account positions from the global `getDepositTokens()`/`getDebtTokens()` lists by checking `balanceOf > 0` (like `invariant_depositTokensOfAccount` does) rather than a separately maintained set that external transfers can desync/fill.

### Proof of Concept
Foundry fork test sketch:

```solidity
// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.9;

import "forge-std/Test.sol";
import {Pool} from "contracts/Pool.sol";
import {DepositToken} from "contracts/DepositToken.sol";

contract ListFillingGriefing_Test is Test {
    Pool pool;
    DepositToken[] depositTokens; // all listed collaterals
    address victim = address(0xV1C);
    address attacker = address(0xA77);

    function setUp() public {
        // fork deployed pool; load pool.getDepositTokens()
    }

    function test_griefVictimDepositList() public {
        // 1. Attacker acquires dust of each deposit token and sends to victim
        uint256 max = pool.MAX_TOKENS_PER_USER(); // via pool storage getter
        for (uint256 i; i < depositTokens.length && pool.getDepositTokensOfAccount(victim).length < max; ++i) {
            depositTokens[i].deposit(1e6, attacker); // or transfer from holder
            vm.prank(attacker);
            depositTokens[i].transfer(victim, 1);
        }
        assertEq(pool.getDepositTokensOfAccount(victim).length, max);

        // 2. Victim tries to deposit a collateral type NOT already in their list
        DepositToken newCollateral = depositTokens[depositTokens.length - 1];
        // (pick one the victim has zero balance of; if all are filled, use any
        //  newly-listed collateral or the debt-token side instead)
        vm.expectRevert(); // MaxNumberOfTokens
        newCollateral.deposit(100e6, victim);
    }
}
```

Note: exact `MAX_TOKENS_PER_USER` constant name/getter and the DebtToken mint path were not fully verified in this pass (grep hits only); the PoC structure and the insertion/revert flow are confirmed in `DepositToken._transfer` (lines 517–525) and `Pool.addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` (lines 204–220).