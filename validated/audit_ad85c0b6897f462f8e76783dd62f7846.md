### Title
Front-runnable `setAllowance` overwrite in `TrustedSpender` allows a delegate to drain old + new allowance (classic ERC20 approve race) - (File: `tare-io__tare-contracts/contracts/TrustedSpender.sol`)

### Summary
`TrustedSpender.setAllowance()` unconditionally overwrites the stored `(amount, validUntil)` for a `(token, from, to)` route, exactly like the vulnerable pre-EIP-738 `ERC20.approve()`. When a Safe reduces (or otherwise changes) an existing non-zero allowance for a route that already has an active delegate, that delegate can front-run the update transaction and drain the old allowance via `executeTransfer`, then drain the newly-set allowance right after it lands — extracting `old + new` instead of the Safe's intended final cap.

### Finding Description
`setAllowance` performs a direct storage overwrite with no check on the pre-existing allowance value: [1](#0-0) 

This mirrors exactly the root cause described in the external report: changing an approval amount from `N` to `M` without first zeroing it lets the already-authorized spender (here, the registered `delegate`, via `executeTransfer`) claim both amounts across the ordering boundary of the update transaction.

`executeTransfer` allows any registered delegate to pull funds up to the currently stored allowance at any time, with no cooldown or nonce tying a transfer to the allowance version it was authorized under: [2](#0-1) 

The spec confirms the overwrite semantics are intentional and unguarded ("Fully overwrites any existing allowance") and the only documented safe way to change a delegate's spending cap without exposure is to `pause()` the contract first: [3](#0-2) [4](#0-3) 

The `safeOrGuardian` path (Safe itself, or a guardian) is the normal, non-privileged-abuse route for a Safe to *reduce* an allowance to an already-registered delegate (e.g., tightening a payroll/vendor route, reacting to a compromised delegate key, or routine limit rebalancing) — this is not a guardian/admin-privilege-abuse scenario; it is the Safe's own honest, unprivileged use of the module being undermined by an already-registered delegate's ability to race the update.

### Impact Explanation
A malicious or compromised delegate can extract `old_allowance + new_allowance` in USDC (or any ERC20 route) from a Safe account instead of being capped at `new_allowance`, directly diverting funds that the Safe explicitly attempted to restrict. Since Safes plug this module into the account/loan-currency flows (delegates are set up for payroll/vendor USDC routes per the spec examples), this is a direct theft/diversion of USDC from a Safe (honest user / shared protocol state), matching the allowed "Theft, diversion, or unauthorized reassignment of USDC" impact.

### Likelihood Explanation
Exploitation requires: (1) a route already has a non-zero allowance and an active delegate, and (2) the Safe (or a guardian) subsequently calls `setAllowance` again for that same route while not pausing the contract first. This is a normal operational action (adjusting/reducing spend caps for an existing delegate) rather than an edge case, and the front-run only requires the attacker (an already-authorized delegate who has turned malicious, or whose key is compromised) to observe the pending mempool transaction and submit `executeTransfer` with higher gas — a standard, low-cost MEV-style front-run.

### Recommendation
Apply the same mitigation recommended in the referenced EIP-738 issue: require the caller to zero out the allowance before setting a new non-zero value, or introduce `increaseAllowance`/`decreaseAllowance`-style delta functions instead of an unconditional overwrite in `setAllowance`. Alternatively, require pausing (or an allowance-version/nonce check consumed by `executeTransfer`) whenever an existing non-zero allowance is being changed for a route with an active delegate, so a delegate's next `executeTransfer` is bound to the exact allowance version it observed.

### Proof of Concept
1. Safe `S` calls `setAllowance(USDC, S, R, 1_000e6, NO_EXPIRY)` and `addDelegate(S, D)`.
2. Later, `S` decides to reduce exposure and submits `setAllowance(USDC, S, R, 100e6, NO_EXPIRY)` (e.g., tightening a payroll cap).
3. Delegate `D` observes this pending tx in the mempool and front-runs it with `executeTransfer(USDC, S, R, 1_000e6)`, draining the full old allowance before the reduction lands (test pattern equivalent to `test_ExecuteTransfer` at [5](#0-4) ).
4. `S`'s `setAllowance` transaction is mined, setting the route's allowance to `100e6`.
5. `D` immediately calls `executeTransfer(USDC, S, R, 100e6)` again, draining the newly-set allowance.
6. Total drained = `1_000e6 + 100e6 = 1_100e6`, exceeding the Safe's intended final cap of `100e6` by the full amount of the stale allowance — reproducing the classic approve/transferFrom race for USDC value under the Safe's control.

### Citations

**File:** tare-io__tare-contracts/contracts/TrustedSpender.sol (L69-81)
```text
  function setAllowance(
    address token,
    address from,
    address to,
    uint208 amount,
    uint48 validUntil
  ) external safeOrGuardian(from) {
    require(token != address(0) && from != address(0) && to != address(0), ZeroAddress());
    require(validUntil > block.timestamp, InvalidAllowanceDeadline());

    _allowances[token][from][to] = Allowance({amount: amount, validUntil: validUntil});
    emit AllowanceSet(token, from, to, amount, validUntil);
  }
```

**File:** tare-io__tare-contracts/contracts/TrustedSpender.sol (L83-99)
```text
  /// @inheritdoc ITrustedSpender
  function executeTransfer(address token, address from, address to, uint256 amount) external whenNotPaused {
    // Verify sender is a delegate
    require(delegates[from][msg.sender], NotADelegate());

    // Check allowance exists, is sufficient, and has not expired
    Allowance storage allowance = _allowances[token][from][to];
    require(allowance.amount >= amount, InsufficientAllowance());
    require(block.timestamp <= allowance.validUntil, AllowanceExpired());

    // Update allowance if not infinite
    if (allowance.amount != type(uint208).max) {
      allowance.amount -= uint208(amount);
    }

    IERC20(token).safeTransferFrom(from, to, amount);
  }
```

**File:** tare-io__tare-contracts/specs/trusted-spender.md (L210-213)
```markdown
**Behavior**:

- Stores amount and validUntil in `_allowances[token][from][to]`
- Fully overwrites any existing allowance (both amount and validUntil)
```

**File:** tare-io__tare-contracts/specs/trusted-spender.md (L550-578)
```markdown
### Emergency Pause

```solidity
// Admin detects suspicious activity
vm.prank(adminAddress);
spender.pause();

// Delegate tries to transfer - this will fail
vm.prank(delegateAddress);
spender.executeTransfer(
    usdcAddress,
    safeAddress,
    vendorAddress,
    100e6  // Reverts with "Contract is paused"
);

// Guardian can still update allowances during pause
vm.prank(guardianAddress);
spender.setAllowance(
    usdcAddress,
    safeAddress,
    vendorAddress,
    500e6,           // Reduced limit
    type(uint48).max // no expiry
);

// Admin resumes operations
vm.prank(adminAddress);
spender.unpause();
```

**File:** tare-io__tare-contracts/test/TrustedSpender.t.sol (L139-160)
```text
  function test_ExecuteTransfer(uint48 validUntil, uint208 transferAmount) public {
    validUntil = uint48(bound(validUntil, timeNow + 1, type(uint48).max));
    transferAmount = uint208(bound(transferAmount, 1, 1000e6));

    vm.prank(safeAccount);
    spender.addDelegate(safeAccount, delegate1);

    vm.prank(safeAccount);
    spender.setAllowance(address(usdc), safeAccount, recipient, 1000e6, validUntil);

    uint256 recipientBalanceBefore = usdc.balanceOf(recipient);
    uint256 safeBalanceBefore = usdc.balanceOf(safeAccount);

    vm.prank(delegate1);
    spender.executeTransfer(address(usdc), safeAccount, recipient, transferAmount);

    assertEq(usdc.balanceOf(recipient), recipientBalanceBefore + transferAmount);
    assertEq(usdc.balanceOf(safeAccount), safeBalanceBefore - transferAmount);

    (uint256 remaining, ) = spender.getAllowance(address(usdc), safeAccount, recipient);
    assertEq(remaining, 1000e6 - transferAmount);
  }
```
