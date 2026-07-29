### Title
Admin can unilaterally remove delegates from any Safe - (`tare-io__tare-contracts/contracts/TrustedSpender.sol`)

### Summary
The `TrustedSpender` and `TrustedCalls` contracts contain a permissioning flaw where the `ADMIN_ROLE` (typically a multisig) can unilaterally remove authorized delegates from any Safe account. This bypasses the intended requirement that such actions should be initiated by the Safe owner or require a timelocked Guardian operation.

### Finding Description
In the Tare protocol, `TrustedSpender` and `TrustedCalls` allow Safe accounts to authorize "delegates" to perform specific actions (like transferring tokens or calling whitelisted functions) on their behalf. 

The `removeDelegate` function in both contracts uses the `safeOrAdmin` modifier: [1](#0-0) 

The `safeOrAdmin` modifier is defined as: [2](#0-1) 

This allows any address with the `ADMIN_ROLE` to call `removeDelegate` for any `safe` address. According to the protocol's security model, the `ADMIN_ROLE` is intended for immediate operational and emergency actions, but sensitive access control changes (like managing delegates) are generally intended to be either user-driven or timelocked via the `GUARDIAN_ROLE`. 

While `addDelegate` correctly requires the Safe's consent or a Guardian's authority: [3](#0-2) 

The `removeDelegate` function grants the Admin (multisig) the power to immediately disable any user's delegate without their consent or a timelock. This matches the root cause described in the external report where a privileged role can unilaterally reset/delete user allocations or delegations.

### Impact Explanation
An compromised or malicious Admin can perform a mass Denial of Service (DoS) by removing all delegates across all Safes in the protocol. This would immediately break automated systems, integrations, or delegated management strategies that Safes rely on for routine operations like loan servicing or vault interactions. While the Admin is a trusted role, the ability to bypass the Safe owner's intent for delegate removal without a timelock exceeds the "least privilege" principle defined in the protocol's own specifications.

### Likelihood Explanation
The likelihood is rated as medium (3/5) because it requires a compromise of the Admin multisig. However, the protocol explicitly documents that `ADMIN_ROLE` should be used for "immediate operational and emergency actions" and implies that delegate management should require multisig/timelock or Safe owner action.

### Recommendation
Restrict `removeDelegate` to only the Safe itself or the `GUARDIAN_ROLE` (which is timelocked). If the Admin requires emergency removal capabilities, it should be clearly documented as a trust assumption, or the function should be moved to the Guardian role to ensure transparency via the TimelockController.

### Proof of Concept
1. A user sets up a Safe and authorizes a delegate via `TrustedSpender.addDelegate`.
2. An attacker gains control of the `ADMIN_ROLE` multisig.
3. The attacker calls `TrustedSpender.removeDelegate(userSafe, userDelegate)`.
4. The `safeOrAdmin` modifier passes because `msg.sender` is the Admin.
5. The delegate is removed, and any subsequent `executeTransfer` calls by that delegate will revert with `NotADelegate()`.

### Citations

**File:** tare-io__tare-contracts/contracts/TrustedSpender.sol (L35-38)
```text
  modifier safeOrAdmin(address safe) {
    require(msg.sender == safe || _isAdminOrGuardian(msg.sender), UnauthorizedCaller());
    _;
  }
```

**File:** tare-io__tare-contracts/contracts/TrustedSpender.sol (L55-60)
```text
  function addDelegate(address safe, address delegate) external safeOrGuardian(safe) {
    require(safe != address(0) && delegate != address(0), ZeroAddress());

    delegates[safe][delegate] = true;
    emit DelegateAdded(safe, delegate);
  }
```

**File:** tare-io__tare-contracts/contracts/TrustedSpender.sol (L63-66)
```text
  function removeDelegate(address safe, address delegate) external safeOrAdmin(safe) {
    delegates[safe][delegate] = false;
    emit DelegateRemoved(safe, delegate);
  }
```
