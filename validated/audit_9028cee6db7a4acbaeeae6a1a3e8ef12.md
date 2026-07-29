### Title
Permanent freeze of originator fee cash if the originator address is blacklisted by the USDC contract - ([File: tare-io__tare-contracts/contracts/Loans.sol])

### Summary
`originatorWithdraw` sweeps every accrued origination fee for a batch of loans into a single hardcoded destination — the loan's registered `originators[loanId]` address — with no way for anyone (not even the guardian/admin) to redirect the payout or change that address after loan creation.

### Finding Description
`originatorWithdraw` computes the net payable origination fee per loan and transfers the aggregated total directly to `originatorAddress` (i.e., `originators[loanId]`), which is fixed at loan creation in `_create` and never has a setter: [1](#0-0) 

Unlike the borrower role (`updateBorrower`, callable by servicer/admin, see [2](#0-1) ) and the servicer role (`updateServicer`, callable by guardian, see [3](#0-2) ), the `originators` mapping set in `_create` has no corresponding `updateOriginator` function anywhere in `Loans.sol` or `ILoans.sol`: [4](#0-3) 

If the originator's on-chain address is blacklisted by the USDC contract (e.g., Circle-level sanction), `currency.safeTransfer(originatorAddress, ...)` in `originatorWithdraw` will always revert, because USDC's `transfer` reverts when the recipient is blacklisted. Because there is no `recipient` parameter and no mechanism to update the stored `originators[loanId]` address, the origination-fee cash for that originator's loans becomes permanently stranded inside the `Loans` contract's ledger (`ACC_ORIGINATOR_FEE_PAYABLE`/`ACC_ORIGINATOR_FEE_PAID`), with no unprivileged or privileged path to release it to the rightful owner. The only theoretical remedy is the guardian-gated `rescueERC20Tokens` in `Rescuable.sol`, which sweeps the contract's entire raw USDC balance to a generic `recoveryAddress` without regard to per-loan cash segregation — breaking the very invariant the ledger is designed to enforce, and still requiring privileged intervention rather than a protocol-native fix: [5](#0-4) 

This mirrors the referenced Beedle finding: a hardcoded transfer recipient with no `recipient` argument and no override mechanism, causing funds to be permanently stuck if that address is blacklisted by the underlying asset.

### Impact Explanation
Origination fee cash (a claimable cashflow belonging to the originator) becomes permanently locked in the `Loans` contract, unrecoverable through any protocol function once the originator address is blacklisted, since neither `originatorWithdraw` nor any admin function can redirect the payout or replace the originator address on existing loans. This is a real, unrecoverable loss of a legitimate value stream, matching the "permanent lock of ... loan cashflows ... caused by an unprivileged path" allowed impact.

### Likelihood Explanation
Low-to-medium likelihood, matching the original report's rating: it requires the originator's wallet to be blacklisted by USDC's issuer, which is a low-probability but realistic event (compliance/sanctions actions on stablecoins are a known occurrence). No attacker action or privileged misuse is needed to trigger it — it is purely an external event colliding with a missing recovery/override mechanism in the protocol.

### Recommendation
Add an `updateOriginator(uint64 loanId, address originator)` guardian/admin function analogous to `updateServicer`, allowing the protocol to redirect future fee payouts to a fresh, non-blacklisted address for affected loans. Additionally, consider adding an optional `recipient` parameter to `originatorWithdraw` (with authorization still tied to `originators[loanId]`) so the fee owner can designate an alternate receiving address without requiring privileged intervention, mirroring the referenced report's suggested fix pattern.

### Proof of Concept
1. Loan is created with `originator = O` via `create()`; the loan is funded and disbursed, accruing an origination fee credited to `ACC_ORIGINATOR_FEE_PAYABLE` for `O`.
2. Before `O` calls `originatorWithdraw`, USDC's issuer blacklists address `O`.
3. `O` (or admin acting on `O`'s behalf via `_requireBatchCaller`) calls `originatorWithdraw([loanId], timestamp, ref)`.
4. The ledger entries are written successfully, but the final `currency.safeTransfer(originatorAddress, uint256(int256(totalTransfer)))` call reverts because USDC blocks transfers to a blacklisted address.
5. The transaction reverts, and there is no function in `Loans.sol` to change `originators[loanId]` or specify an alternate recipient — the accrued fee remains permanently stuck in `ACC_ORIGINATOR_FEE_PAYABLE`/`ACC_ORIGINATOR_FEE_PAID` for that loan, with the only escape hatch being the guardian's indiscriminate `rescueERC20Tokens`, which breaks per-loan cash segregation for all other loans sharing the pooled USDC balance.

### Citations

**File:** tare-io__tare-contracts/contracts/Loans.sol (L161-163)
```text
    borrowers[loanId] = borrower;
    servicers[loanId] = servicer;
    originators[loanId] = originator;
```

**File:** tare-io__tare-contracts/contracts/Loans.sol (L203-214)
```text
  function updateBorrower(
    uint64 loanId,
    address borrower
  ) external whenNotPaused onlyServicerOrAdmin(loanId) notTerminal(loanId) {
    require(borrower != address(0), ZeroAddress());
    require(isRegisteredForRole(servicers[loanId], Roles.Borrower, borrower), UnregisteredAddress(borrower));

    borrowers[loanId] = borrower;
    data[loanId].updatedAt = uint48(block.timestamp);

    emit LoanBorrowerUpdated(loanId, borrower);
  }
```

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

**File:** tare-io__tare-contracts/contracts/Loans.sol (L806-841)
```text
  function originatorWithdraw(
    uint64[] calldata loanIds,
    uint48 timestamp,
    bytes32 ref
  ) external whenNotPaused nonReentrant returns (OriginatorWithdrawalResult[] memory results) {
    uint256 numLoans = loanIds.length;
    results = new OriginatorWithdrawalResult[](numLoans);

    int128 totalTransfer = 0;
    address originatorAddress;
    uint64 currentLoanCount = loanCount;

    for (uint256 i = 0; i < numLoans; ++i) {
      uint64 loanId = loanIds[i];

      require(loanId != 0 && loanId <= currentLoanCount, DoesNotExist());

      originatorAddress = _requireBatchCaller(originators[loanId], i, originatorAddress);

      int128 amount = _getNetPayable(loanId, ACC_ORIGINATOR_FEE_PAYABLE, ACC_ORIGINATOR_FEE_PAID);

      totalTransfer += _withdrawToAccount(
        loanId,
        ACC_ORIGINATOR_FEE_PAID,
        amount,
        timestamp,
        ENTRY_ORIGINATOR_FEE_WITHDRAWAL,
        ref
      );

      results[i] = OriginatorWithdrawalResult({loanId: loanId, amount: amount});

      data[loanId].updatedAt = timestamp;
    }

    currency.safeTransfer(originatorAddress, uint256(int256(totalTransfer)));
```

**File:** tare-io__tare-contracts/contracts/misc/Rescuable.sol (L28-40)
```text
  /// @inheritdoc IRescuable
  function rescueERC20Tokens(
    address token,
    uint256 amount
  ) external whenNotPaused onlyRole(GUARDIAN_ROLE) returns (uint256 rescued) {
    require(recoveryAddress != address(0), RecoveryAddressNotSet());
    uint256 balance = IERC20(token).balanceOf(address(this));
    rescued = amount >= balance ? balance : amount;
    if (rescued > 0) {
      IERC20(token).safeTransfer(recoveryAddress, rescued);
      emit ERC20TokensRescued(token, rescued, recoveryAddress);
    }
  }
```
