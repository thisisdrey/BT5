[1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** tare-io__tare-contracts/specs/loan_status_lifecycle.md (L38-53)
```markdown
| `_onlyOutstanding` | `status == Active || status == ChargedOff` | Allows only active servicing states `Active` and `ChargedOff`. |
| `_onlyOutstandingOrFullyPaid` | `status == Active || status == ChargedOff || status == FullyPaid` | Allows servicing states and `FullyPaid` (e.g. residual payment waterfall). |

### External/public functions with status-gating behavior

| Function | Status check(s) used | Allowed statuses (status dimension only) |
|---|---|---|
| `updateBorrower` | `notTerminal` | `Created`, `FullyFunded`, `Active`, `FullyPaid`, `ChargedOff` |
| `updateServicer` | `notTerminal` | `Created`, `FullyFunded`, `Active`, `FullyPaid`, `ChargedOff` |
| `pay` | `onlyOutstanding` | `Active`, `ChargedOff` |
| `accrue` | `onlyOutstanding` | `Active`, `ChargedOff` |
| `chargeMiscFee` | `onlyOutstanding` | `Active`, `ChargedOff` |
| `fund` | `status == Created` | `Created` only |
| `disburse` | `status == FullyFunded` | `FullyFunded` only |
| `applyWaterfall` | `onlyOutstandingOrFullyPaid` | `Active`, `ChargedOff`, `FullyPaid` |
| `returnFunds` | `onlyOutstandingOrFullyPaid` | `Active`, `ChargedOff`, `FullyPaid` |
```

**File:** tare-io__tare-contracts/specs/loan_status_lifecycle.md (L63-78)
```markdown
## Status-by-Status Call Matrix

Legend: `Y` means status check allows it, `N` means status check blocks it.

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

**File:** tare-io__tare-contracts/contracts/Loans.sol (L23-64)
```text
import {
  ENTRY_LOAN_COMMITMENT,
  ENTRY_INVESTOR_CAPITAL_RECEIVED,
  ENTRY_BORROWER_PAYMENT,
  ENTRY_INTEREST_ACCRUAL,
  ENTRY_BORROWER_PRINCIPAL_PAYMENT,
  ENTRY_DISBURSEMENT_TO_BORROWER,
  ENTRY_ORIGINATOR_FEE_WITHHOLDING,
  ENTRY_SERVICER_FEE_ALLOCATION,
  ENTRY_INVESTOR_INTEREST_ALLOCATION,
  ENTRY_BORROWER_INTEREST_DEBT_CLEARANCE,
  ENTRY_SERVICER_FEE_WITHDRAWAL,
  ENTRY_INVESTOR_INTEREST_WITHDRAWAL,
  ENTRY_INVESTOR_PRINCIPAL_WITHDRAWAL,
  ENTRY_MISC_FEE_CHARGE,
  ENTRY_MISC_FEE_DEBT_CLEARANCE,
  ENTRY_MISC_FEE_WITHDRAWAL,
  ENTRY_ORIGINATOR_FEE_WITHDRAWAL
} from "contracts/interfaces/LedgerEntries.sol";
import {
  ACC_BORROWER_INTEREST_PAID,
  ACC_BORROWER_MISC_FEE_PAID,
  ACC_BORROWER_INTEREST_RECEIVABLE,
  ACC_BORROWER_MISC_FEE_RECEIVABLE,
  ACC_BORROWER_PAYMENT_CLEARING,
  ACC_BORROWER_PRINCIPAL_RECEIVABLE,
  ACC_BORROWER_PRINCIPAL_REPAID,
  ACC_CASH,
  ACC_INVESTOR_INTEREST_PAID,
  ACC_INVESTOR_INTEREST_PAYABLE,
  ACC_INVESTOR_PRINCIPAL_PAYABLE,
  ACC_INVESTOR_PRINCIPAL_REPAID,
  ACC_ORIGINATOR_FEE_PAID,
  ACC_ORIGINATOR_FEE_PAYABLE,
  ACC_SERVICER_ADJUSTMENT,
  ACC_SERVICER_FEE_PAID,
  ACC_SERVICER_FEE_PAYABLE,
  ACC_SERVICER_MISC_FEE_PAID,
  ACC_SERVICER_MISC_FEE_PAYABLE,
  ACC_UNALLOCATED_BORROWER_INTEREST_PAYABLE,
  ACC_UNFUNDED_COMMITMENT
} from "contracts/interfaces/Accounts.sol";
```
