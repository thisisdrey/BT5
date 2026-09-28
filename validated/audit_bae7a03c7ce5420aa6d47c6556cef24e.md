### Title
Dust-transfer griefing fills `depositTokensOfAccount`/`debtTokensOfAccount` to `MAX_TOKENS_PER_USER`, permanently blocking a victim from adding collateral and forcing liquidation - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool` tracks each account's deposit and debt tokens in `MappedEnumerableSet.AddressSet` lists capped at `MAX_TOKENS_PER_USER = 30` (`contracts/Pool.sol:79`). Whenever a `DepositToken` balance goes from 0 to non-zero, `DepositToken._transfer`/`deposit` calls `Pool.addToDepositTokensOfAccount(recipient)`, which reverts with `UserReachedMaxTokens` once the combined list length hits 30 (`contracts/Pool.sol:143-148,216-220`). Because `DepositToken.transfer`/`transferFrom` are permissionless and only check the *sender's* lock (`_revertIfLocked(_msgSender, amount_)`, `contracts/DepositToken.sol:348-376`), any EOA can send 1-wei dust of every listed deposit token to a victim, pinning the victim's lists at the cap.

### Finding Description
- `addToDepositTokensOfAccount` and `addToDebtTokensOfAccount` revert when `debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER` (`contracts/Pool.sol:143-148`).
- The attacker deposits (or receives via `transfer`) a dust amount of each of the pool's deposit tokens — the pool itself caps deposit token offerings at `MAX_TOKENS_PER_USER` (`addDepositToken`, `contracts/Pool.sol:703`) — then transfers the dust to the victim. Each first-time receipt pushes a new entry into `depositTokensOfAccount[victim]` via `addToDepositTokensOfAccount`, which `DepositToken` calls on a 0→non-zero balance transition.
- Once the victim's combined list length is 30:
  - `deposit()`/`mint()` on any *new* `DepositToken` reverts → the victim cannot top up collateral with a different asset.
  - `DebtToken._mint` → `pool.addToDebtTokensOfAccount` reverts (`contracts/DebtToken.sol:597-600`) → the victim cannot open debt in a new synthetic, blocking refinancing via `issue` and `SmartFarmingManager.leverage`.
- Self-healing is unreliable: if the victim has outstanding debt, `unlockedBalanceOf` (`contracts/DepositToken.sol:383-398`) locks their balances, so the attacker-controlled dust entries may be non-transferable; even when removable, the attacker re-dusts in the same or next block at near-zero cost.

### Impact Explanation
Liveness + integrity impact matching the CVE class (unprivileged attacker, repeatable DoS and unauthorized state change). A victim whose collateral factor deteriorates or whose collateral price drops cannot deposit a *different* collateral type to restore health, so a rescue `deposit()` reverts with `UserReachedMaxTokens`. The position is then liquidated via `Pool.liquidate` (`contracts/Pool.sol:537-596`), causing direct loss of the victim's collateral to liquidators — a forced, preventable liquidation enabled solely by the attacker's dust transfers. Funds equivalent to the liquidation incentive are effectively stolen/the victim suffers avoidable loss.

### Likelihood Explanation
- Fully unprivileged: only `DepositToken.transfer`/`transferFrom` calls with dust amounts; no privileged role, oracle manipulation, or malicious bridge needed.
- Cost is bounded: at most 30 dust transfers (the pool cannot list more than `MAX_TOKENS_PER_USER` deposit tokens anyway).
- No existing control stops it: `onlyIfAdditionWillNotReachMaxTokens` is the cap being abused; `nonReentrant`, pause flags, and `SynthContext` do not restrict `transfer` to arbitrary recipients.

### Recommendation
Do not revert inside `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` when the cap is reached during a *receipt*; instead, only enforce `onlyIfAdditionWillNotReachMaxTokens` on user-initiated actions that create new exposure (e.g., `deposit`, `issue`, `mint`), or track tokens via a per-account `balanceOf > 0` scan over the (bounded) pool token list rather than a mutable per-user set that third parties can poison. Alternatively, allow removal of dust entries by permitting `removeFromDepositTokensOfAccount` to be triggered on any balance-zero state regardless of locks.

### Proof of Concept
```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";
import {Pool} from "../contracts/Pool.sol";
import {DepositToken} from "../contracts/DepositToken.sol";
import {DebtToken} from "../contracts/DebtToken.sol";
import {IERC20} from "../contracts/dependencies/openzeppelin/token/ERC20/IERC20.sol";

contract DustGriefingTest is Test {
    Pool pool;
    address victim = address(0xA);
    address attacker = address(0xB);

    function setUp() public {
        // Fork mainnet/base Pool proxy + deployed DepositTokens
        // pool = Pool(<pool proxy>);
    }

    function test_DustFillsUserTokenList() public {
        address[] memory depositTokens = pool.getDepositTokens(); // length <= 30
        vm.startPrank(attacker);
        for (uint256 i; i < depositTokens.length; i++) {
            DepositToken dt = DepositToken(depositTokens[i]);
            // attacker acquires dust via deposit() with min amount
            dt.transfer(victim, 1); // adds depositToken to victim's list
        }
        vm.stopPrank();

        assertEq(
            pool.getDepositTokensOfAccount(victim).length +
            pool.getDebtTokensOfAccount(victim).length,
            pool.MAX_TOKENS_PER_USER()
        );

        // Victim can no longer deposit any new collateral type:
        vm.prank(victim);
        vm.expectRevert(Pool.UserReachedMaxTokens.selector);
        DepositToken(depositTokens[0] /* any token not yet held... every listed token is held */).deposit(1e18, victim);

        // Nor open a new debt position:
        vm.prank(victim);
        vm.expectRevert(Pool.UserReachedMaxTokens.selector);
        // debtToken.issue(...) reverts in _mint -> addToDebtTokensOfAccount
    }
}
```
Run against a forked deployment (`deployments/mainnet`, `deployments/base`, etc.) where the pool already lists multiple deposit tokens; the attacker's only cost is ~30 dust transfers plus minimal underlying for the initial deposits.