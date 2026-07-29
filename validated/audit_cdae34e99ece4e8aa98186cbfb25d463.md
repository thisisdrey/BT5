### Title
Unaccounted Bad Debt Loss During Partial Liquidation/Repayment of Charged-Off Loans - ([File: tare-io__tare-contracts/contracts/Loans.sol])

### Summary
The Tare protocol lacks a mechanism to account for and realize bad debt losses when a loan is partially repaid or liquidated while in a `ChargedOff` status. When a loan is marked as `ChargedOff`, the `NavCalculator` applies a discount factor to the outstanding principal to estimate its value. However, the protocol's ledger maintains the full original principal receivable balance. If a liquidator or borrower makes a partial payment that is less than the total outstanding debt, and the loan is subsequently closed or remains insolvent, the difference (bad debt) is never formally written off the ledger. This causes the Net Asset Value (NAV) of the `PortfolioVault` to remain inflated by the "unreturned principal" that will never be collected, leading to unfair share pricing and potential losses for remaining vault users.

### Finding Description
In the Tare protocol, a loan's financial state is managed by a double-entry ledger in `Loans.sol` [1](#0-0) . When a loan becomes insolvent, the servicer can update its status to `ChargedOff` [2](#0-1) . This status allows continued servicing, such as receiving payments via `pay` [3](#0-2)  and allocating them via `applyWaterfall` [4](#0-3) .

The root cause is that `applyWaterfall` only allows clearing debt up to the amount actually paid by the borrower [5](#0-4) . There is no "write-off" function that allows the servicer to reduce the `ACC_BORROWER_PRINCIPAL_RECEIVABLE` balance without a corresponding payment. Although `createLedgerEntries` exists [6](#0-5) , it explicitly blocks any entries involving `ACC_CASH` [7](#0-6) , meaning it cannot be used to reconcile the loss against the investor's capital accounts in a way that the `NavCalculator` recognizes as a finalized loss.

The `NavCalculator` computes loan value as `unreturnedInvestorPrincipal * bucketFactor + collectedCash` [8](#0-7) . If a loan is `ChargedOff`, it uses the `FACTOR_CHARGED_OFF` (e.g., 25%) [9](#0-8) . However, because the ledger principal balance is never reduced to reflect the actual unrecoverable loss, the `unreturnedInvestorPrincipal` remains high. If a final partial payment is made and the loan is moved to `Closed`, the `Closed` factor (often 0) is applied to the *entire* remaining balance [10](#0-9) , but until that terminal transition, the "bad debt" portion continues to drag on the NAV calculation.

### Impact Explanation
The lack of bad debt accounting leads to material corruption of the vault's NAV. Specifically:
1. **NAV Inflation**: The `PortfolioVault` NAV includes the discounted value of principal that the protocol knows is unrecoverable but hasn't "written off" the ledger.
2. **Unfair Share Pricing**: New investors might mint shares at an inflated price, or exiting investors might redeem at a price that doesn't reflect the realized loss of an insolvent position.
3. **Inaccurate Financial Reporting**: The ledger state remains "unbalanced" with respect to the reality of the loan's recoverability, violating the expectation of per-loan cash segregation and accurate investor entitlement.

### Likelihood Explanation
The likelihood is high because the protocol explicitly supports `ChargedOff` as a non-terminal state where payments and waterfalls still occur [11](#0-10) . Any loan that undergoes a partial liquidation or a "settle-for-less" agreement will result in this state where the ledger principal exceeds the recoverable amount.

### Recommendation
Implement a `writeOffBadDebt` function in `Loans.sol` that allows a servicer to move balances from `ACC_BORROWER_PRINCIPAL_RECEIVABLE` to a dedicated `ACC_BAD_DEBT_REALIZED` account (a contra-asset or expense account). This would reduce the `outstandingInvestorPrincipal` reported by `getLoanValues` [12](#0-11) , allowing the `NavCalculator` and the `PortfolioVault` to reflect the loss accurately and immediately.

### Proof of Concept
1. A loan is created with 10,000 USDC principal.
2. The borrower becomes insolvent; the servicer sets the status to `ChargedOff`.
3. The `NavCalculator` applies a 25% factor, so the loan contributes 2,500 USDC to the Vault's NAV.
4. A liquidator repays 1,000 USDC as a final partial settlement.
5. The servicer calls `applyWaterfall` for the 1,000 USDC. The ledger now shows 9,000 USDC as "outstanding principal".
6. The `NavCalculator` now values the loan at `9,000 * 0.25 + 0 = 2,250 USDC` (assuming the 1,000 was withdrawn).
7. In reality, the 8,000 USDC difference is bad debt and will never be recovered. The NAV should reflect only the cash collected, but it continues to include 2,250 USDC of "ghost value" because the 8,000 USDC was never formally written off the ledger's receivable account. [13](#0-12)

### Citations

**File:** tare-io__tare-contracts/contracts/Loans.sol (L10-15)
```text
import {LoansLedger} from "./LoansLedger.sol";
import {
  ILoans,
  LoanData,
  LoanTerms,
  LoanStatus,
```

**File:** tare-io__tare-contracts/contracts/Loans.sol (L235-243)
```text
  function updateLoanData(
    uint64 loanId,
    LoanStatus status,
    uint48 nextDueDate,
    uint48 maturityDate,
    uint48 timestamp
  ) external whenNotPaused onlyServicerOrAdmin(loanId) loanExists(loanId) withLoanUpdate(loanId, timestamp) {
    _updateLoanData(loanId, status, nextDueDate, maturityDate);
  }
```

**File:** tare-io__tare-contracts/contracts/Loans.sol (L307-307)
```text
    onlyOutstanding(loanId)
```

**File:** tare-io__tare-contracts/contracts/Loans.sol (L338-339)
```text
        outstandingInvestorPrincipal: -_getAccountBalance(loanId, ACC_INVESTOR_PRINCIPAL_PAYABLE) -
          _getAccountBalance(loanId, ACC_INVESTOR_PRINCIPAL_REPAID),
```

**File:** tare-io__tare-contracts/contracts/Loans.sol (L615-615)
```text
    onlyOutstandingOrFullyPaid(loanId)
```

**File:** tare-io__tare-contracts/contracts/Loans.sol (L625-629)
```text
    require(
      miscFees + servicingFees + investorInterest + principal <=
        -_getAccountBalance(loanId, ACC_BORROWER_PAYMENT_CLEARING),
      InvalidAmount()
    );
```

**File:** tare-io__tare-contracts/contracts/Loans.sol (L741-751)
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
```

**File:** tare-io__tare-contracts/contracts/Loans.sol (L758-758)
```text
      require(e.from != ACC_CASH && e.to != ACC_CASH, InvalidAccount());
```

**File:** tare-io__tare-contracts/specs/vault.md (L234-238)
```markdown
Net Asset Value is calculated on-chain by aggregating across all loan NFTs held by the vault. Per-loan values are read from the authoritative Loans contract ledger, then valued by the external calculator — collected investor cash at par and unreturned investor principal adjusted by a bucket factor (see [nav-calculator.md](nav-calculator.md)):

$TotalAssets = \text{Idle Currency Balance} - \text{Total Pending Deposit Assets} - \text{Total Claimable Redeem Assets} + \sum_{i=1}^{n} \text{LoanValue}_i$

Where each $\text{LoanValue}_i$ is computed by the external [calculator contract](#valuation-strategy) as `unreturnedInvestorPrincipal * bucketFactor + collectedCash`.
```

**File:** tare-io__tare-contracts/test/NavCalculator.t.sol (L105-125)
```text
  function test_GetLoansValue_UsesChargedOffFactor_WhenLoanIsChargedOff() public {
    uint64 id = _createLoanWithInvestorCashflow(PRINCIPAL, bytes32("ref2"));

    // Charge off the loan via updateLoanData
    vm.prank(servicer);
    loans.updateLoanData({
      loanId: id,
      status: LoanStatus.ChargedOff,
      nextDueDate: 0,
      maturityDate: 0,
      timestamp: timeNow
    });

    uint64[] memory ids = new uint64[](1);
    ids[0] = id;

    uint256 result = calculator.getLoansValue(ILoans(address(loans)), ids);
    uint256 expected = _expectedLoanValue(id, FACTOR_CHARGED_OFF);

    assertEq(result, expected, "ChargedOff factor not applied correctly");
  }
```

**File:** tare-io__tare-contracts/specs/nav-calculator.md (L50-55)
```markdown
- **ChargedOff / Closed / Cancelled**: The bucket factor for that terminal status applies regardless of DPD. The factor is applied only to the residual `unreturnedInvestorPrincipal`; `collectedCash` is always added at par. The servicer can flip a loan to `Closed` or `Cancelled` while residual principal remains (e.g. settled-for-less, mid-funding cancellation), so these factors express recoverability of the residual rather than being inert.
- **Active**: The DPD-based bucket factor applies to `unreturnedInvestorPrincipal`; `collectedCash` is added at par.
- **`Created`**: Contributes 0 naturally — the investor has not yet funded the commitment, so `outstandingInvestorPrincipal` and `investorPrincipalWithdrawable` are both zero and the formula yields 0 without a special case.
- **All other statuses** (`FullyPaid`, `FullyFunded`): Valued at 100% with no discount. This prevents stale `nextDueDate` values from incorrectly discounting loans that have no credit risk. `FullyPaid` loans in particular may still carry undistributed cash claimable by the investor; that cash is captured by `collectedCash`.

Closed and Cancelled default to `0`: the factor writes off the residual unreturned principal while `collectedCash` is still honored at par.
```

**File:** tare-io__tare-contracts/specs/loan_status_lifecycle.md (L67-78)
```markdown
| Function | Created | FullyFunded | Active | FullyPaid | Cancelled | ChargedOff | Closed |
|---|---:|---:|---:|---:|---:|---:|---:|
| `updateBorrower` | Y | Y | Y | Y | N | Y | N |
| `updateServicer` | Y | Y | Y | Y | N | Y | N |
| `pay` | N | N | Y | N | N | Y | N |
| `accrue` | N | N | Y | N | N | Y | N |
| `chargeMiscFee` | N | N | Y | N | N | Y | N |
| `fund` | Y | N | N | N | N | N | N |
| `disburse` | N | Y | N | N | N | N | N |
| `applyWaterfall` | N | N | Y | Y | N | Y | N |
| `returnFunds` | N | N | Y | Y | N | Y | N |
| `refundBorrower` | N | N | Y | Y | N | Y | N |
```

**File:** tare-io__tare-contracts/test/Loans/unit/Loans_GetLoanValues.t.sol (L248-251)
```text
  // After charge-off, getLoanValues must still report the ledger truth: outstanding investor
  // principal stays at the unrecovered amount, withdrawable cash reflects whatever has been
  // collected but not yet pulled, and the status flips to ChargedOff. NavCalculator depends on
  // this split to apply the ChargedOff bucket factor only to the credit-exposed portion.
```
