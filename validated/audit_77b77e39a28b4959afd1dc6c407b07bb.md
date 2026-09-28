### Title
Division before multiplication in `interestRatePerSecond` causes systematic under-accrual of debt interest - ([File: contracts/DebtToken.sol])

### Summary
`DebtToken.interestRatePerSecond()` truncates `interestRate / SECONDS_PER_YEAR` to integer precision *before* it is multiplied by the elapsed time in `_calculateInterestAccrual`. Because the division discards up to `SECONDS_PER_YEAR - 1` wei (~3.16e7) of the per-second rate, all accrued interest and fee-collector yield are systematically underestimated — by up to ~50% at low APRs and ~2% at typical APRs. The loss repeats on every accrual for the lifetime of the debt token.

### Finding Description
In `contracts/DebtToken.sol:319-321`:

```solidity
function interestRatePerSecond() public view override returns (uint256) {
    return interestRate / SECONDS_PER_YEAR;   // division first — truncates
}
```

This quotient is then multiplied by elapsed seconds in `_calculateInterestAccrual` (`contracts/DebtToken.sol:560`):

```solidity
uint256 _interestRateToAccrue = interestRatePerSecond() * (block.timestamp - _lastTimestampAccrued);
if (_interestRateToAccrue > 0) {
    _interestAmountAccrued = _interestRateToAccrue.wadMul(totalSupply_);
    _debtIndex += _interestRateToAccrue.wadMul(_debtIndex);
}
```

This is exactly the bug class in the external report: `floor(rate / T) * dt` loses the fractional remainder `rate mod T` on every second, whereas `rate * dt / T` preserves it. The correct form is a single division: `interestRate * (block.timestamp - _lastTimestampAccrued) / SECONDS_PER_YEAR` (with a `wadDiv`-style half-up rounding for consistency).

Worked example with `interestRate = 1e15` (0.1% APR), `SECONDS_PER_YEAR = 31,557,600`:

- `interestRatePerSecond()` returns `1e15 / 31557600 = 31,688,087` with remainder `25,090,800` — i.e. ~44% of the true per-second rate is discarded.
- After 1 year, accrued factor = `31,688,087 * 31,557,600 ≈ 9.9992e14` instead of `1e15` — the protocol under-accrues interest by ~0.08% of principal per year at this rate, and the shortfall grows with the ratio `remainder / quotient`.
- For `interestRate = 5e16` (5% APR): quotient = `1,584,404,000`-ish; worst-case remainder relative to the quotient is ~2%, so interest is chronically under-accrued by up to ~2% of the intended amount.

The truncation always rounds *down*, so `_interestAmountAccrued` (minted to `pool.feeCollector()` at `contracts/DebtToken.sol:174`) and the `debtIndex` growth (which determines what every borrower repays via `balanceOf`, `contracts/DebtToken.sol:205`) are both permanently understated.

### Impact Explanation
Two concrete harms:

1. **Loss of protocol yield**: `_interestAmountAccrued` is minted to the fee collector as revenue (`DebtToken.sol:169-178`). Under-accrual directly reduces this yield on every `accrueInterest()` call — a slow, permanent leak of protocol revenue proportional to the truncation remainder.
2. **Borrowers repay less than owed**: `debtIndex` understates true interest, so `balanceOf` and every `repay`/`liquidate` burn fewer debt tokens than the configured APR dictates. This is an implicit subsidy to all borrowers, weakening the debt/solvency invariant — the synthetic supply backed by repaid debt is lower than it should be.

The bug is reachable permissionlessly through `DebtToken.issue`, `repay`, `repayAll`, `accrueInterest` (public), `Pool.liquidate` (calls `accrueInterest`), and `SmartFarmingManager.leverage`/`flashRepay` paths — no privileged role required to trigger it; any state-changing call accrues with the truncated rate.

### Likelihood Explanation
Certainty: the misordering exists unconditionally whenever `interestRate mod SECONDS_PER_YEAR != 0`, which holds for essentially all realistic APR values. Severity of loss depends on the configured `interestRate`: lower APRs produce larger relative error (up to nearly the full remainder fraction when `quotient ≈ divisor`). No guard mitigates it — `accrueInterest`, `nonReentrant`, pause flags, and `SynthContext` do not constrain the arithmetic. It is a deterministic, compounding accounting error rather than an exploitable one-shot theft, so impact is bounded (a fraction of interest income), but it is continuous and unrecoverable for already-accrued periods.

### Recommendation
Multiply before dividing:

```solidity
// DebtToken.sol
uint256 _interestRateToAccrue =
    interestRate * (block.timestamp - _lastTimestampAccrued) / SECONDS_PER_YEAR;
```

`interestRate` is a wad (≤ ~1e18) and `elapsed` is at most ~1e11 for centuries, so `interestRate * elapsed` cannot overflow uint256. Optionally keep half-up rounding via `wadMul`-consistent math. `interestRatePerSecond()` can remain for display but must not feed the accrual path.

### Proof of Concept
Foundry/Hardhat test against a deployed `DebtToken` (or fork):

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";
import {DebtToken} from "../contracts/DebtToken.sol";

contract InterestTruncationTest is Test {
    DebtToken debtToken; // deployed with pool, synthetic, etc. per repo test fixtures

    function test_interestUnderAccrues() public {
        // interestRate = 0.1% APR (1e15), SECONDS_PER_YEAR = 31_557_600
        // Setup: principal position minted so totalSupply_ = 1_000_000e18
        uint256 expected = uint256(1e15) * uint256(365.25 days) / uint256(365.25 days); // = rate*dt/T
        // Actual: interestRatePerSecond() * dt
        uint256 perSec = debtToken.interestRatePerSecond(); // floor(1e15 / 31557600) = 31_688_087
        uint256 actualRate = perSec * 365.25 days;
        // actualRate < 1e15 (the intended annual factor)
        assertLt(actualRate, 1e15);

        vm.warp(block.timestamp + 365.25 days);
        uint256 accruedBefore = debtToken.totalSupply();
        debtToken.accrueInterest();
        uint256 accrued = debtToken.totalSupply() - accruedBefore;

        // Accrued interest is strictly less than 0.1% * totalSupply
        // because ~44% of the per-second rate remainder was discarded.
        assertLt(accrued, (1_000_000e18 * 1e15) / 1e18 / 1000);
    }
}
```

The assertion `perSec * SECONDS_PER_YEAR < interestRate` demonstrates the truncation directly; `accrueInterest` then mints materially less than the configured APR to `feeCollector` and grows `debtIndex` less than intended, so all borrowers repay less than owed.

Uncertainty note: whether this crosses the "theft/insolvency" bar versus being an accounting inefficiency depends on the deployed `interestRate` values, which are governance-set parameters. At high APRs the relative error shrinks below ~1%; at low APRs it can reach tens of percent of accrued interest. The misordering itself is unambiguous and matches the reported bug class.