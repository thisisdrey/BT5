### Title
Dust-transfer griefing fills a victim's per-account token list to `MAX_TOKENS_PER_USER`, permanently blocking new collateral deposits and debt issuance while the position has debt - ([File: contracts/DepositToken.sol])

### Summary
`Pool` enforces a hard cap of `MAX_TOKENS_PER_USER = 30` on the combined length of an account's `depositTokensOfAccount` and `debtTokensOfAccount` sets. Every `DepositToken._transfer` adds the token to the recipient's set when their balance moves 0 → non-zero, with no opt-in and no minimum amount. An unprivileged attacker can dust 1 wei of every listed deposit token to a victim, filling all 30 slots. From then on, `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` revert with `UserReachedMaxTokens`, so the victim cannot deposit any new collateral type, cannot receive seized/new deposit tokens, and cannot be issued any new debt token. Critically, a victim with outstanding debt cannot evict the dust entries because `transfer`/`transferFrom`/`withdraw` all call `_revertIfLocked`, which returns `unlockedBalanceOf == 0` when `issuableInUsd == 0`, making the denial persistent for the life of the debt.

### Finding Description
The bug class is resource-exhaustion DoS: like CVE-2011-2189's per-connection namespace exhaustion, here each deposit-token entry is a finite per-account "slot" that a third party can force-allocate at dust cost.

Relevant code:

- `Pool.sol:79` `uint256 public constant MAX_TOKENS_PER_USER = 30;`
- `Pool.sol:143-148` `onlyIfAdditionWillNotReachMaxTokens` reverts when `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30`.
- `Pool.sol:216-220` `addToDepositTokensOfAccount(account)` is callable by any registered deposit token; `_transfer`/`_mint`/`seize` all trigger it for recipients (`DepositToken.sol:518-520`, `486-488`, `343-345`).
- `DepositToken.sol:348-376` `transfer`/`transferFrom` call `_revertIfLocked(sender, amount)` — anyone can push tokens *to* a victim (lock only constrains the sender).
- `DepositToken.sol:383-398` `unlockedBalanceOf` returns 0 for every deposit token once `debtInUsd > 0` and `issuableInUsd == 0`, so a victim who is fully collateralized-utilized cannot transfer the dust back out to free the slots.

Attack trace (all unprivileged):
1. Victim has an open position (deposit + debt) with, say, 2 entries used, and `issuableInUsd == 0` (normal after full borrow, or after a small adverse price move).
2. Attacker deposits a minimal amount into each of the 28 other registered deposit tokens via `DepositToken.deposit` (cost: dust + deposit fee, recoverable by withdrawing their own balance afterwards), then `transfer(victim, 1)` on each. Each call executes `pool.addToDepositTokensOfAccount(victim)`; victim's set reaches 30.
3. Any subsequent path that adds a *new* token to the victim's lists reverts:
   - `DepositToken.deposit` / `Pool.deposit` of a new collateral → `_mint` → `addToDepositTokensOfAccount` → `UserReachedMaxTokens`.
   - `DebtToken.issue`/SmartFarming leverage minting a new synth → `addToDebtTokensOfAccount` → revert.
   - Anyone transferring the victim a deposit token they don't already hold → `_transfer` → revert.
4. Victim attempts to evict dust: `transfer` reverts via `_revertIfLocked` because `unlockedBalanceOf(victim) == 0` while `debtInUsd > 0` and `issuableInUsd == 0`. `withdraw`/`withdrawFrom` likewise revert. The victim cannot call `removeFromDepositTokensOfAccount` (deposit-token-only callers, `Pool.sol:_revertIfSenderIsNotDepositToken`).

The victim's only escapes are repaying debt (to regain `issuableInUsd > 0`) or being liquidated — both impossible if they intended to restore health by depositing a *different* collateral, which is precisely the action that reverts.

### Impact Explanation
Liveness / temporary-to-persistent freezing of funds and position management. A targeted borrower is denied the standard remedy for a deteriorating position: topping up a new collateral type. Once price drift pushes them toward liquidation, they cannot deposit any collateral outside the ≤30 dusted-in tokens, cannot issue new synthetics, and cannot even receive protocol-relevant token transfers. This can convert a recoverable position into forced liquidation losses for the user and potential bad debt for the protocol. An attacker can also pre-fill the lists of contracts or known depositors (e.g., SmartFarming-related accounts, integrators) to brick their future interactions.

### Likelihood Explanation
- Requires no privileged role: only public `deposit`, `transfer`, and pool entry points; attacker supplies their own collateral.
- Cost is bounded: at most 30 dust deposits + transfers, and the underlying is fully recoverable by withdrawing from their own balance (attacker's account, no debt, dust is unlocked).
- Works on any pool deployment (deployed `Pool` on mainnet/base/optimism/hemi/bsc/swell/plasma all ship `MAX_TOKENS_PER_USER = 30`).
- No guard prevents it: `addToDepositTokensOfAccount` has no `onlyIfCan...` recipient check, no minimum-amount threshold, and there is no user-side opt-out or admin eviction path.
- Constraint: impact is worst against accounts that already carry debt with `issuableInUsd == 0`; a debt-free victim can clear slots themselves, reducing the attack to a gas-grief. Still qualifies as temporary freezing of funds/liveness.

### Recommendation
- Add a minimum balance threshold (e.g., dust floor per underlying's decimals) before `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` is invoked in `_mint`/`_transfer`/`seize`.
- Allow a user to force-remove a token from their own list when its balance is below the dust threshold (e.g., `purgeToken(address)` calling `removeFromDepositTokensOfAccount` on their own behalf), independent of `_revertIfLocked`.
- Alternatively, make `unlockedBalanceOf` never lock below the eviction amount, or exempt "send full balance to self/zero-out" transfers from the lock check.
- Consider raising `MAX_TOKENS_PER_USER` is insufficient alone; the fix must break the forced-allocation primitive.

### Proof of Concept
Foundry fork test sketch (Hardhat equivalent works against `deployments/mainnet/Pool.json` addresses):

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;
import "forge-std/Test.sol";
import {Pool} from "contracts/Pool.sol";
import {DepositToken} from "contracts/DepositToken.sol";
import {IERC20} from "contracts/dependencies/openzeppelin/token/ERC20/IERC20.sol";

contract DustGriefTest is Test {
    Pool pool = Pool(POOL_PROXY);
    address attacker = address(0xA77);
    address victim; // existing borrower: debtInUsd > 0, issuableInUsd == 0

    function test_fillVictimTokenList() public {
        address[] memory dtokens = pool.getDepositTokens();
        uint256 slots = pool.MAX_TOKENS_PER_USER()
            - pool.getDepositTokensOfAccount(victim).length
            - pool.getDebtTokensOfAccount(victim).length;

        vm.startPrank(attacker);
        for (uint256 i; i < dtokens.length && slots > 0; ++i) {
            DepositToken dt = DepositToken(dtokens[i]);
            if (dt.balanceOf(victim) > 0) continue; // already occupies a slot
            IERC20 underlying = dt.underlying();
            deal(address(underlying), attacker, 1e6);
            underlying.approve(address(dt), type(uint256).max);
            dt.deposit(1e6, attacker);          // mint msdTOKEN to attacker
            dt.transfer(victim, 1);             // dust -> forces addToDepositTokensOfAccount(victim)
            slots--;
        }
        vm.stopPrank();

        assertEq(
            pool.getDepositTokensOfAccount(victim).length + pool.getDebtTokensOfAccount(victim).length,
            pool.MAX_TOKENS_PER_USER()
        );

        // Victim can no longer deposit a new collateral type
        DepositToken newToken = DepositToken(dtokens[0]); // one not already in victim's set
        vm.startPrank(victim);
        deal(address(newToken.underlying()), victim, 1 ether);
        newToken.underlying().approve(address(newToken), type(uint256).max);
        vm.expectRevert(Pool.UserReachedMaxTokens.selector);
        newToken.deposit(1 ether, victim);

        // Victim cannot evict the dust while locked (debtInUsd>0 && issuableInUsd==0)
        DepositToken dust = DepositToken(pool.getDepositTokensOfAccount(victim)[0]);
        vm.expectRevert(DepositToken.NotEnoughFreeBalance.selector);
        dust.transfer(attacker, 1);
        vm.stopPrank();
    }
}
```

Choose `victim` as a real borrower whose `debtPositionOf` returns `_issuableInUsd == 0` on the forked block, or construct one in-test: deposit collateral, `DebtToken.issue` up to the limit, then run the attack. The revert on `deposit` demonstrates denial of new collateral; the revert on `transfer` demonstrates the victim cannot self-heal while in debt.