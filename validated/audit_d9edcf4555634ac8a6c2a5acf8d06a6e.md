[1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** tare-io__tare-contracts/contracts/NavCalculator.sol (L78-94)
```text
      // Cash already collected for the investor — principal and waterfall-allocated interest sitting
      // in Loans.sol awaiting withdrawal — has no credit risk and contributes at par.
      int256 collectedCash = int256(loanData.investorPrincipalWithdrawable) +
        int256(loanData.investorInterestWithdrawable);
      if (collectedCash < 0) collectedCash = 0;

      // Investor principal still out with the borrower — the only portion exposed to credit risk
      // and the only portion the bucket factor applies to.
      int256 unreturnedInvestorPrincipal = int256(loanData.outstandingInvestorPrincipal) -
        int256(loanData.investorPrincipalWithdrawable);
      if (unreturnedInvestorPrincipal < 0) unreturnedInvestorPrincipal = 0;

      uint256 factoredPrincipal = (uint256(unreturnedInvestorPrincipal) *
        _bucketFactor(loanData.status, loanData.nextDueDate)) / WAD_UNIT;

      totalValue += factoredPrincipal + uint256(collectedCash);
    }
```

**File:** tare-io__tare-contracts/test/NavCalculator.t.sol (L524-579)
```text
  function test_GetLoansValue_ClampsNegativeCollectedCash_ToZero() public {
    // Defensive clamp: collectedCash = principalWithdrawable + interestWithdrawable; if the
    // ledger ever produces a net-negative sum, NAV must treat it as 0 rather than wrapping.
    uint64[] memory ids = new uint64[](1);
    ids[0] = 7;

    LoanValue[] memory mocked = new LoanValue[](1);
    mocked[0] = LoanValue({
      outstandingInvestorPrincipal: 100_000e6,
      investorPrincipalWithdrawable: -40_000e6,
      investorInterestWithdrawable: -10_000e6,
      status: LoanStatus.Active,
      nextDueDate: uint48(block.timestamp + 30 days)
    });

    vm.mockCall(address(loans), abi.encodeWithSelector(loans.getLoanValues.selector, ids), abi.encode(mocked));

    // unreturnedInvestorPrincipal = 100k - (-40k) = 140k
    // collectedCash = -40k + -10k = -50k → clamped to 0
    // expected = 140k * 1.0 + 0
    uint256 expected = 140_000e6;
    assertEq(
      calculator.getLoansValue(ILoans(address(loans)), ids),
      expected,
      "Negative collected cash must clamp to zero"
    );
  }

  function test_GetLoansValue_ClampsNegativeUnreturnedPrincipal_ToZero() public {
    // Defensive clamp: unreturned = outstandingInvestorPrincipal - principalWithdrawable; if the
    // withdrawable exceeds the outstanding (e.g. borrower overpayment allocated as principal),
    // the difference must be clamped to 0 rather than wrapping under uint cast.
    uint64[] memory ids = new uint64[](1);
    ids[0] = 8;

    LoanValue[] memory mocked = new LoanValue[](1);
    mocked[0] = LoanValue({
      outstandingInvestorPrincipal: 10_000e6,
      investorPrincipalWithdrawable: 30_000e6,
      investorInterestWithdrawable: 5_000e6,
      status: LoanStatus.Active,
      nextDueDate: uint48(block.timestamp + 30 days)
    });

    vm.mockCall(address(loans), abi.encodeWithSelector(loans.getLoanValues.selector, ids), abi.encode(mocked));

    // unreturned = 10k - 30k = -20k → clamped to 0
    // collectedCash = 30k + 5k = 35k
    // expected = 0 * 1.0 + 35k
    uint256 expected = 35_000e6;
    assertEq(
      calculator.getLoansValue(ILoans(address(loans)), ids),
      expected,
      "Negative unreturned principal must clamp to zero"
    );
  }
```

**File:** tare-io__tare-contracts/test/helpers/NavMath.sol (L1-16)
```text
// SPDX-License-Identifier: BUSL-1.1
pragma solidity 0.8.33;

import {LoanValue} from "contracts/interfaces/ILoans.sol";

/// @notice Decomposes a LoanValue into the two inputs the NAV formula consumes:
/// `principal` (credit-exposed, discounted by the bucket factor) and `cash`
/// (already collected for the investor, valued at par). Mirrors the
/// int256-intermediate + clamp pattern used in NavCalculator.sol.
function splitLoanValue(LoanValue memory value) pure returns (uint256 principal, uint256 cash) {
  int256 principalSigned = int256(value.outstandingInvestorPrincipal) - int256(value.investorPrincipalWithdrawable);
  principal = principalSigned > 0 ? uint256(principalSigned) : 0;

  int256 cashSigned = int256(value.investorPrincipalWithdrawable) + int256(value.investorInterestWithdrawable);
  cash = cashSigned > 0 ? uint256(cashSigned) : 0;
}
```
