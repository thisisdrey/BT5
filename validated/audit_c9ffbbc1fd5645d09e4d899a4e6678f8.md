### Title
Unauthorized Reassignment of Investor Entitlements via Unvalidated Ledger Entries - (`tare-io__tare-contracts/contracts/Loans.sol`)

### Summary
The `Loans.sol` contract allows servicers to create arbitrary ledger entries between internal accounts (excluding `ACC_CASH`) through the `createLedgerEntries` function. This function lacks validation against loan terms, status, or actual payment evidence, enabling a servicer to divert funds intended for investors to themselves or other protocol roles.

### Finding Description
In `Loans.sol`, the `createLedgerEntries` function is implemented as a manual correction mechanism for servicers [1](#0-0) . While it prevents direct manipulation of the `ACC_CASH` account [2](#0-1) , it does not place any restrictions on moving balances between other ledger accounts [3](#0-2) . 

The protocol's accounting relies on these internal balances to determine withdrawal entitlements. For example, `servicerWithdraw` calculates the payout based on the net balance of `ACC_SERVICER_FEE_PAYABLE` and `ACC_SERVICER_FEE_PAID` [4](#0-3) . Similarly, investor withdrawals are driven by the balances in `ACC_INVESTOR_PRINCIPAL_PAYABLE` and `ACC_INVESTOR_INTEREST_PAYABLE` [5](#0-4) . 

Because `createLedgerEntries` allows a servicer to arbitrarily transfer amounts from an investor-owned liability account to a servicer-owned liability account, a malicious servicer can "steal" the right to future or existing cashflows without actually moving `ACC_CASH` at the time of the ledger update. This is analogous to the reported bug where a relayer could set arbitrary parameters to influence trade outcomes; here, the servicer sets arbitrary ledger parameters to influence value distribution.

### Impact Explanation
This leads to the unauthorized diversion of USDC and loan cashflows from honest investors to a malicious servicer. By reassigning the protocol's internal state, the servicer can bypass the intended waterfall logic and extract value that was materially corrupted at the ledger level. This violates the core invariant of per-loan cash segregation and investor entitlement.

### Likelihood Explanation
High. The `servicer` role is a per-loan privileged role that is expected to perform these actions, and the `createLedgerEntries` function is a reachable, unprivileged path (from the perspective of the global protocol) that directly enables the attack.

### Recommendation
Restrict `createLedgerEntries` to only allow transfers between specific "safe" correction accounts, or implement a validation layer that ensures the sum of investor and originator entitlements remains constant. Alternatively, require `ADMIN_ROLE` or `GUARDIAN_ROLE` for any ledger entries that debit investor-related accounts.

### Proof of Concept
1. A loan is created and funded with 10,000 USDC. `ACC_INVESTOR_PRINCIPAL_PAYABLE` balance is -10,000 (liability).
2. The loan is disbursed. The borrower eventually pays back 5,000 USDC via `pay()`, which sits in `ACC_BORROWER_PAYMENT_CLEARING`.
3. A malicious servicer calls `createLedgerEntries` for the `loanId` with the following `LedgerEntryInput`:
   - `from`: `ACC_INVESTOR_PRINCIPAL_PAYABLE`
   - `to`: `ACC_SERVICER_FEE_PAYABLE`
   - `amount`: 5,000
4. The `LoansLedger._updateBalances` function executes, increasing the servicer's payable fee balance by 5,000 and decreasing the investor's principal payable by 5,000.
5. The servicer calls `applyWaterfall` to move the 5,000 USDC from `ACC_BORROWER_PAYMENT_CLEARING` to the newly inflated `ACC_SERVICER_FEE_PAYABLE` (via the intermediate clearance accounts).
6. The servicer calls `servicerWithdraw` and receives the 5,000 USDC, which was legally owed to the investor as principal repayment.

### Citations

**File:** tare-io__tare-contracts/contracts/Loans.sol (L680-681)
```text
      int128 servicingFee = _getNetPayable(loanId, ACC_SERVICER_FEE_PAYABLE, ACC_SERVICER_FEE_PAID);
      int128 miscFee = _getNetPayable(loanId, ACC_SERVICER_MISC_FEE_PAYABLE, ACC_SERVICER_MISC_FEE_PAID);
```

**File:** tare-io__tare-contracts/contracts/Loans.sol (L741-752)
```text
  function createLedgerEntries(
    uint64 loanId,
    uint48 timestamp,
    LedgerEntryInput[] calldata ledgerEntries
  )
    external
    whenNotPaused
    onlyServicerOrAdmin(loanId)
    loanExists(loanId)
    withLoanUpdate(loanId, timestamp)
    returns (uint128[] memory entryIndices)
  {
```

**File:** tare-io__tare-contracts/contracts/Loans.sol (L758-758)
```text
      require(e.from != ACC_CASH && e.to != ACC_CASH, InvalidAccount());
```

**File:** tare-io__tare-contracts/contracts/Loans.sol (L759-759)
```text
      entryIndices[i] = _createInternalEntry(loanId, e.from, e.to, e.amount, timestamp, e.entryType, e.ref);
```

**File:** tare-io__tare-contracts/contracts/LoansLedger.sol (L130-142)
```text
  function _getNetInterestPayableToInvestor(uint64 loanId) internal view returns (int128) {
    return _getNetPayable(loanId, ACC_INVESTOR_INTEREST_PAYABLE, ACC_INVESTOR_INTEREST_PAID);
  }

  /**
   * @dev Convenience accessor for the net principal currently payable to the loan's
   *      investor: principal already repaid by the borrower, minus principal already
   *      paid out to the investor. This is bounded by borrower repayments and is not
   *      the investor's full remaining principal claim.
   */
  function _getNetPrincipalPayableToInvestor(uint64 loanId) internal view returns (int128) {
    return _getNetPayable(loanId, ACC_BORROWER_PRINCIPAL_REPAID, ACC_INVESTOR_PRINCIPAL_REPAID);
  }
```
