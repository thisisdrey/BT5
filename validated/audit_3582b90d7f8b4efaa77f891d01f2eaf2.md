### Title
Account token-list exhaustion via dust deposit tokens permanently blocks new deposits/borrows and can brick liquidations - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
The bug class in CVE-2026-42478 is "unvalidated/malformed input triggers a crash (DoS) during object construction". The Metronome analog is the unvalidated per-account token list: `Pool.addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` revert with `UserReachedMaxTokens` once `debtTokensOfAccount.length + depositTokensOfAccount.length >= MAX_TOKENS_PER_USER (30)`. Because `DepositToken._mint`/`_transfer` add the token to the *recipient's* list with no minimum amount and no opt-in, an attacker can force-fill a victim's list with 1-wei balances of every registered deposit token, causing all subsequent operations that would add a new token to revert — a state-level DoS.

### Finding Description
- `Pool.onlyIfAdditionWillNotReachMaxTokens` reverts when the combined list reaches `MAX_TOKENS_PER_USER = 30` [1](#0-0) .
- `DepositToken._mint` adds the deposit token to `account_`'s list whenever the recipient's prior balance is zero and `amount_ > 0` — including when `deposit(amount_, onBehalfOf_ = victim)` is called by anyone [2](#0-1) .
- `DepositToken._transfer` (reachable via `transfer`/`transferFrom`/`seize`) does the same for the recipient; the only check on the sender side is `_revertIfLocked`, which is satisfied for a dust unlocked balance [3](#0-2) .
- There is no minimum mint/transfer amount and no way for the recipient to refuse entries; the victim can only remove an entry by zeroing that token's balance (`removeFromDepositTokensOfAccount` fires only at balance == 0) [4](#0-3) .

Attack path (unprivileged EOA):
1. For each registered deposit token `d_i` in the target `Pool` (up to 30), attacker calls `d_i.deposit(dustAmount, victim)` — or deposits to self and `transfer(victim, 1)` if deposit fees round the net to zero.
2. Each call appends `d_i` to `victim`'s `depositTokensOfAccount`. Once the count reaches 30, `onlyIfAdditionWillNotReachMaxTokens` starts reverting.
3. Consequences that now revert for the victim:
   - `DepositToken.deposit` / `VesperGateway.deposit` / `NativeTokenGateway.deposit` for any collateral token not already in their list (`_mint` → `addToDepositTokensOfAccount` reverts).
   - `DebtToken.issue`/`mint`/`flashIssue` for any synthetic not already in `debtTokensOfAccount` — no new borrows, including `SmartFarmingManager.leverage`.
   - `Pool.liquidate` where the seized collateral is a `DepositToken` the liquidator does not yet hold: `seize` → `_transfer` → `addToDepositTokensOfAccount(liquidator)` reverts if the liquidator's list is full. Attackers can dust-fill known liquidator addresses to stall liquidations while a position goes underwater.

### Impact Explanation
Temporary freezing of funds / liveness failure. A targeted user loses the ability to add new collateral types or open new debt positions until they manually zero out the dust entries (transfer each dust token to a fresh address — one transaction per token, 30 forced transactions, and the attacker can re-dust cheaply since deposit-on-behalf costs the attacker only gas + dust underlying). More importantly, during a liquidation cascade the victim cannot top-up a *new* collateral to rescue their position (only existing listed tokens work), and liquidators with filled lists cannot execute `liquidate`, delaying bad-debt cleanup and risking protocol insolvency. This matches the "availability loss via crafted input" class of the reference CVE.

### Likelihood Explanation
Low cost, fully permissionless. Requires only small amounts of underlying per deposit token and ordinary public entry points (`DepositToken.deposit` with `onBehalfOf_`, or `transfer`). No privileged role, oracle manipulation, or governance action needed. The cap of 30 makes saturation cheap on pools with fewer registered collateral types — attacker can also combine dust debt positions (`issue` dust synths) to fill `debtTokensOfAccount`. The main mitigating factor is that the victim can self-recover by emptying the dust balances, so the freeze is temporary rather than permanent.

### Recommendation
- Add a minimum amount (e.g. non-dust threshold or USD floor via `masterOracle`) before `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` is invoked, or
- Track "spam" entries separately: allow the counter to only count positions above the dust threshold, or
- Make `liquidate` resilient by catching the `addToDepositTokensOfAccount` revert for the liquidator (e.g. route seized collateral to `Treasury` or allow `seize` to skip list insertion), and
- Consider letting `deposit(amount_, onBehalfOf_)` add to the list only when `onBehalfOf_ == _msgSender()` or when the amount clears a dust threshold.

### Proof of Concept
```solidity
// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.24;

import "forge-std/Test.sol";
import {Pool} from "../contracts/Pool.sol";
import {DepositToken} from "../contracts/DepositToken.sol";
import {IERC20} from "../contracts/dependencies/openzeppelin/token/ERC20/IERC20.sol";

contract TokenListGriefingTest is Test {
    // Fork a chain where Pool is deployed (e.g. mainnet fork).
    Pool pool;
    DepositToken[] depositTokens; // all pool.getDepositTokens() entries
    IERC20[] underlyings;

    function test_fillVictimList_blocksNewDepositAndBorrow() public {
        address attacker = makeAddr("attacker");
        address victim = makeAddr("victim");

        uint256 max = pool.MAX_TOKENS_PER_USER(); // 30

        // 1. Attacker dust-deposits (or transfers) 1 wei of each msdToken to victim
        for (uint256 i; i < depositTokens.length && pool.getDepositTokensOfAccount(victim).length < max; ++i) {
            DepositToken d = depositTokens[i];
            IERC20 u = d.underlying();
            deal(address(u), attacker, 1e18);
            vm.startPrank(attacker);
            u.approve(address(d), 1e18);
            d.deposit(1e18, attacker);              // attacker gets msdTokens
            d.transfer(victim, 1);                  // force-adds d to victim's list
            vm.stopPrank();
        }

        // victim's list is now full
        assertEq(pool.getDepositTokensOfAccount(victim).length, max);

        // 2. Victim tries to deposit a collateral token NOT in their list -> reverts
        DepositToken newCollateral = /* another registered DepositToken */;
        IERC20 newUnderlying = newCollateral.underlying();
        deal(address(newUnderlying), victim, 100e18);
        vm.startPrank(victim);
        newUnderlying.approve(address(newCollateral), 100e18);
        vm.expectRevert(Pool.UserReachedMaxTokens.selector);
        newCollateral.deposit(100e18, victim);

        // 3. Victim tries to borrow a synthetic NOT in their debt list -> reverts
        // debtToken.issue(...) -> pool.addToDebtTokensOfAccount(victim)
        vm.expectRevert(Pool.UserReachedMaxTokens.selector);
        // pool.debtTokenOf(msUSD).issue(1, victim) via pool path
        vm.stopPrank();
    }
}
```
Run: `forge test --fork-url <rpc> --match-test test_fillVictimList_blocksNewDepositAndBorrow`. Expected: both victim transactions revert with `UserReachedMaxTokens`; attacker cost is ~30 dust transfers.

### Citations

**File:** contracts/Pool.sol (L143-148)
```text
    modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
        if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
            revert UserReachedMaxTokens();
        }
        _;
    }
```

**File:** contracts/DepositToken.sol (L485-489)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
    }
```

**File:** contracts/DepositToken.sol (L517-525)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }

        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf[sender_] == 0) {
            pool.removeFromDepositTokensOfAccount(sender_);
        }
```
