### Title
Unprivileged dust transfers fill an account's token list to `MAX_TOKENS_PER_USER`, permanently DoSing deposits, withdrawals, and liquidations that pay fees to that account (File: contracts/Pool.sol)

### Summary
Every `DepositToken` balance change calls `Pool.addToDepositTokensOfAccount(account_)`, which reverts once the account's combined deposit+debt token list reaches `MAX_TOKENS_PER_USER = 30`. Anyone can grow *any other account's* list simply by `transfer`ring dust amounts of deposit tokens to it. Filling the `feeCollector`'s list makes every fee-bearing protocol operation revert with `UserReachedMaxTokens`, freezing user funds and blocking liquidations.

### Finding Description
`DepositToken._transfer` adds the deposit token to the recipient's per-account list whenever the recipient previously held zero:

- `contracts/DepositToken.sol:518-520` — `if (_recipientBalanceBefore == 0 && amount_ > 0) pool.addToDepositTokensOfAccount(recipient_)`
- `contracts/Pool.sol:143-148` — `onlyIfAdditionWillNotReachMaxTokens` reverts `UserReachedMaxTokens` when `debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= 30`
- `contracts/Pool.sol:216-220` — `addToDepositTokensOfAccount` enforces the cap on behalf of the *recipient*, who never consented.

An attacker sends 1-wei transfers of deposit tokens the target doesn't hold. The target's list grows by one per distinct token, with no opt-out at add time.

The critical victim is `poolRegistry.feeCollector()`, because three state-changing paths unconditionally add to *its* list:

1. `DepositToken.deposit` → `_mint(feeCollector, fee)` → `addToDepositTokensOfAccount(feeCollector)` (`DepositToken.sol:231, 486-487`)
2. `DepositToken.withdraw`/`withdrawFrom`/`flashWithdraw` → `_withdraw` → `_transfer(account, feeCollector, fee)` → add (`DepositToken.sol:547`)
3. `Pool.liquidate` → `depositToken_.seize(account_, feeCollector, _fee)` → `_transfer` → add (`Pool.sol:591-592`)

Once `feeCollector`'s list hits 30 (attacker dusts the deposit tokens it doesn't already hold; the pool can register up to 30 deposit tokens via `addDepositToken`), any deposit, withdrawal, or liquidation whose fee token is *not already in* the list reverts. The same technique can also be applied to any user to block their deposits into new collateral types and new borrows (`addToDebtTokensOfAccount` shares the same 30-slot budget via `DebtToken` mint).

### Impact Explanation
While `feeCollector`'s list is saturated and a nonzero `depositFee`/`withdrawFee`/`protocolFee` is configured, all deposits and withdrawals that would mint/transfer a fee in a not-yet-listed token revert, temporarily freezing user collateral, and `Pool.liquidate` reverts for the same reason, allowing unhealthy positions to accrue bad debt — protocol insolvency and liveness failure. Per-user saturation blocks a victim from adding collateral or new debt positions until they manually clear entries. This is the direct analog of CVE-2018-6536: an unprivileged party writes an entry (the token list membership — the "PID") into a file the victim doesn't control, and a later privileged-context action (fee collection during deposit/withdraw/liquidate — the "kill") fails or acts on the poisoned state.

### Likelihood Explanation
The attack requires only ERC20 dust transfers — no flash loans, governance, or privileged access. Cost is a handful of gas-only transactions. Feasibility depends on the deployed configuration: it works cleanly when the pool has enough registered deposit tokens to push `feeCollector` to 30 (up to 30 are allowed), or when `feeCollector`'s list is already close to the cap through normal fee accrual. For ordinary users the DoS is only temporary because they can transfer the dust back out (the entries are removable once balance hits zero), but `feeCollector` is a contract/multisig that may not promptly clear its list, and entries for tokens it legitimately accrues cannot be avoided.

### Recommendation
Do not gate the *recipient-side* add on a hard cap, or make `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` non-reverting for `seize`/fee flows (e.g., skip tracking when the recipient is `feeCollector`, or return a boolean instead of reverting). Alternatively, exempt `feeCollector` from the cap, or let anyone call a permissionless `removeFromDepositTokensOfAccount` on behalf of an account for tokens with zero/low balance.

### Proof of Concept
```solidity
// Foundry fork test sketch
function test_FeeCollectorListSaturationDoS() public {
    // pool, depositTokens[] deployed; fees nonzero
    address feeCollector = poolRegistry.feeCollector();

    // 1. Attacker deposits dust in each deposit token the feeCollector
    //    doesn't yet hold, then transfers the minted msdTOKEN dust to it.
    for (uint i; i < depositTokens.length; ++i) {
        IDepositToken dt = depositTokens[i];
        if (dt.balanceOf(feeCollector) == 0) {
            underlying[i].approve(address(dt), 1);
            dt.deposit(1, attacker);          // mint dust to attacker
            dt.transfer(feeCollector, 1);     // +1 entry in feeCollector list
        }
    }
    assertGe(
        pool.getDepositTokensOfAccount(feeCollector).length +
        pool.getDebtTokensOfAccount(feeCollector).length,
        pool.MAX_TOKENS_PER_USER()
    );

    // 2. Any deposit/withdraw in a token the collector doesn't already
    //    track reverts; liquidations paying protocol fee revert.
    vm.expectRevert(UserReachedMaxTokens.selector);
    depositToken.deposit(amount, alice);       // _mint(feeCollector, fee) reverts

    vm.expectRevert(UserReachedMaxTokens.selector);
    depositToken.withdraw(amount, alice);      // _transfer(..., feeCollector, fee) reverts

    vm.expectRevert(UserReachedMaxTokens.selector);
    pool.liquidate(msToken, unhealthyAcct, repayAmt, depositToken);
}
```

Notes: requires a configured nonzero `depositFee`/`withdrawFee`/`protocolFee` (check `FeeProvider` on the target chain) and enough distinct pool deposit tokens to reach the cap for `feeCollector`. The per-user variant (blocking a victim's new deposits/borrows) works with as few as `30 - currentListLength(victim)` dust transfers and needs no fee configuration.