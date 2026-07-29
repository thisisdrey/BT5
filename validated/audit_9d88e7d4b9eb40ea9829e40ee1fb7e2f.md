### Title
Revoking a compromised servicer's approval does not stop them from continuing to control every loan already assigned to them - ([File: contracts/misc/LoansAuth.sol / contracts/Loans.sol])

### Summary
This is the same root cause as the reported "staking key enabled status not checked" issue: an entity's *revocable/enabled* status is authoritative at grant time but is never re-checked at the points where that entity actually exercises value-moving privileges. In Tare, `revokeServicer` is the designated "disable a compromised key" mechanism for servicers, but `Loans` never re-validates the canonical (`address(this)`) servicer-approval bit when a servicer acts on a loan — it only checks `servicers[loanId] == msg.sender`.

### Finding Description
`LoansAuth.approveServicer`/`revokeServicer` toggle a bit in the canonical address book (`addressBook[address(this)][user]`), and this bit is only consulted in `Loans.updateServicer` at the moment a new servicer is assigned to a loan: [1](#0-0) 

All of the servicer's actual operational privileges over an already-assigned loan — `pay`, `accrue`, `chargeMiscFee`, `applyWaterfall`, `servicerWithdraw`, `refundBorrower`, `returnFunds`, `createLedgerEntries` — are gated purely by `_onlyServicerOrAdmin`, which only compares `msg.sender` to the stored `servicers[loanId]` address and never consults `isRegisteredForRole(address(this), Roles.Servicer, servicer)`: [2](#0-1) 

So `revokeServicer(user)` merely flips the canonical bit off: [3](#0-2) 

but has **zero effect** on any loan where that address is already recorded in `servicers[loanId]`. Exactly as in the external report — where the approver service failed to re-check the Cubist "enabled" flag before honoring a signature from an already-compromised key — Tare's admin/guardian "disable this compromised servicer" action (`revokeServicer`) does not propagate to the actual authorization check (`_onlyServicerOrAdmin`) used at every fund-moving entry point. The only way to actually stop a compromised servicer is to call `updateServicer(loanId, newServicer)` individually for every loan they control, which requires the guardian to enumerate and know all affected loan IDs in real time during an active-compromise incident.

### Impact Explanation
If a servicer's operational hot key is compromised, the documented incident-response action (`revokeServicer`, analogous to disabling the Cubist key) provides no real-time protection: the compromised key can still call `pay`, `applyWaterfall`, `servicerWithdraw`, `refundBorrower`, and `returnFunds` on every loan it was already assigned to, continuing to divert loan cashflows (servicer fees, misallocated waterfall entries, ledger corrections) out of per-loan cash segregation until each loan is individually re-serviced. This directly threatens "material corruption of ... per-loan cash segregation ... that produces real value loss" — one of the explicitly allowed impacts — because the revocation control that is supposed to gate this value movement is silently bypassed for all pre-existing assignments.

### Likelihood Explanation
Likelihood is moderate-to-high in an incident scenario: this is precisely the situation `revokeServicer` exists to handle (a compromised servicer hot key), and the gap is deterministic — not an edge case — any active loan under a just-revoked servicer remains fully exploitable with no additional preconditions beyond already holding that hot key.

### Recommendation
Re-check `isRegisteredForRole(address(this), Roles.Servicer, servicers[loanId])` (or an equivalent still-approved check) inside `_onlyServicerOrAdmin`/the servicer-gated entry points, so that revoking a servicer's canonical approval immediately disables their control over all loans currently assigned to them, not just future assignments.

### Proof of Concept
1. Guardian calls `approveServicer(servicerHot)`; `servicerHot` is assigned as `servicers[loanId]` on many active loans via `create`.
2. `servicerHot`'s key is compromised.
3. Admin/guardian calls `revokeServicer(servicerHot)` believing this disables the compromised key immediately, as it does for `create`/`updateServicer`.
4. Attacker, still controlling `servicerHot`, calls `pay(loanId, ...)`, `applyWaterfall(loanId, ...)`, and `servicerWithdraw(loanId, ...)` on any of the pre-existing loans — all succeed because `_onlyServicerOrAdmin` only checks `servicers[loanId] == msg.sender`, ignoring the now-revoked canonical approval bit. [4](#0-3)

### Citations

**File:** tare-io__tare-contracts/contracts/Loans.sol (L221-232)
```text
  function updateServicer(
    uint64 loanId,
    address servicer
  ) external whenNotPaused onlyRole(GUARDIAN_ROLE) notTerminal(loanId) {
    require(servicer != address(0), ZeroAddress());
    require(isRegisteredForRole(address(this), Roles.Servicer, servicer), UnregisteredAddress(servicer));

    servicers[loanId] = servicer;
    data[loanId].updatedAt = uint48(block.timestamp);

    emit LoanServicerUpdated(loanId, servicer);
  }
```

**File:** tare-io__tare-contracts/contracts/Loans.sol (L968-982)
```text
  function _onlyServicerOrAdmin(uint64 loanId) internal view {
    _requireCallerOrAdmin(servicers[loanId]);
  }

  function _onlyBorrowerOrAdmin(uint64 loanId) internal view {
    _requireCallerOrAdmin(borrowers[loanId]);
  }

  function _withLoanUpdate(uint64 loanId, uint48 timestamp) internal {
    data[loanId].updatedAt = timestamp;
  }

  function _requireCallerOrAdmin(address addr) private view {
    require(addr == msg.sender || _isAdminOrGuardian(msg.sender), Unauthorized());
  }
```

**File:** tare-io__tare-contracts/contracts/misc/LoansAuth.sol (L89-94)
```text
  /// @inheritdoc ILoansAuth
  function revokeServicer(address user) public onlyAdminOrGuardian {
    addressBook[address(this)][user] &= ~SERVICER_MASK;
    emit AddressUnregistered(address(this), Roles.Servicer, user);
    emit ServicerRevoked(user);
  }
```
