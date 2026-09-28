### Title
Compounding-frequency-dependent linear interest accrual makes effective borrower APR unpredictable and attacker-influenceable - (File: contracts/DebtToken.sol)

### Summary
`DebtToken._calculateInterestAccrual` computes interest linearly over the elapsed time (`interestRatePerSecond() * (block.timestamp - lastTimestampAccrued)`) applied to the already-grown `totalSupply_`/`debtIndex`. Because `accrueInterest()` is permissionless and folds accrued interest into the compounding base on every call, the effective interest a borrower pays depends on how often accrual is triggered — ranging from the simple (linear) APR if accrual happens once per period, up to a continuously-compounded rate if it happens every second. Any unprivileged EOA can call `accrueInterest()` each block to force the maximum compounding curve on all borrowers.

### Finding Description
In `contracts/DebtToken.sol` (Lauraivanka/metronome-synth-public--010): [1](#0-0) 

```solidity
uint256 _interestRateToAccrue = interestRatePerSecond() * (block.timestamp - _lastTimestampAccrued);
if (_interestRateToAccrue > 0) {
    _interestAmountAccrued = _interestRateToAccrue.wadMul(totalSupply_);
    _debtIndex += _interestRateToAccrue.wadMul(_debtIndex);
}
```

Interest is computed linearly on `totalSupply_`, and both `totalSupply_` and `debtIndex` are then updated to include the accrued amount (`accrueInterest`, lines 156-180). User debt is `principalOf[account] * debtIndex / debtIndexOf[account]` (`balanceOf`, lines 196-206), so each accrual compounds all prior accruals.

Consequences:

- If `accrueInterest` runs once a year, a borrower pays exactly `interestRate` (e.g. 10%).
- If it runs every second (a public, unauthenticated call), the borrower pays `e^r - 1` ≈ 10.52% for r=10%, ≈ 64.9% for r=50%, etc. Metronome's tests (`test/DebtToken.test.ts` lines 816-830) confirm multi-period compounding behavior (10% + 50% over two years → 165 on 100 principal).
- The "advertised" APR (`interestRate`, used in `debtOf`, liquidation `debtFloorInUsd` checks, and displayed rates) therefore does not correspond to the actual rate charged; the real rate is a function of accrual call frequency, which is outside both borrowers' and the protocol's control.

Nothing stops the frequent-accrual path: `accrueInterest()` has no access-control, no `whenNotPaused`/`whenNotShutdown` gating, and no reentrancy guard, and the early-return only fires when called twice in the same block. Every pool operation (`issue`, `mint`, `flashIssue`, `repay`, `burn`, `deposit`-side calls) also accrues, so on a live deployment the effective rate sits near the compounding maximum — but a borrower evaluating `interestRate` sees only the linear APR.

### Impact Explanation
- **Uncertainty / rate mismatch:** The effective borrow cost is unbounded between the linear APR and the continuously-compounded equivalent; neither borrowers nor the protocol can predict the exact figure in advance.
- **Attacker-influenceable debt inflation:** Any EOA can keep `accrueInterest()` pinged every block, forcing the maximum compounding trajectory on the entire `debtIndex`. This marginally inflates every borrower's `balanceOf` and pushes marginal positions' health factors below the liquidation threshold faster than the stated APR implies, enabling the attacker to capture liquidation proceeds sooner. The excess interest is minted as synthetic tokens to `pool.feeCollector()`, so the protocol's fee intake is also frequency-dependent.
- At high `interestRate` values the gap between min and max effective rate is material (e.g. ~15% relative at 50% APR), not just rounding dust.

### Likelihood Explanation
Likelihood of the max-compounding state is high in practice — it is the default on any active pool since every user interaction accrues — but the *incremental* impact of an attacker forcing it is bounded by the gap between "natural" accrual frequency and per-block accrual. Severity is moderate: the invariant "debt grows exactly at `interestRate` APR" is broken, debt/health-factor projections are unreliable, and the phenomenon is fully reachable by unprivileged callers, but the absolute excess vs. a busy pool's natural accrual is small for low APRs and only becomes significant at high rates or on dormant pools.

### Recommendation
Compute interest so the result is independent of accrual frequency:

- Use a per-second exponential compounding factor, e.g. store `interestFactorPerSecond` and compute `debtIndex = debtIndex * rpow(factor, elapsed, 1e27)` (MakerDAO-style `rpow`), or fix `interestRatePerSecond` and apply `debtIndex * (1e18 + ratePerSec)^elapsed`.
- Document that `interestRate` is a nominal APR vs. effective APY, or switch to quoting the compounded rate so integrators and borrowers see the true number.

### Proof of Concept
Foundry-style test against the repo's own `DebtToken` (pattern mirrors `test/DebtToken.test.ts::accrueInterest`):

```solidity
function test_AccrualFrequencyDependency() public {
    // Setup (hardhat fixtures equivalent):
    // user deposits MET collateral, issues 100 msUSD debt, interestRate = 0.5e18 (50% APR)

    // Path A: single accrual after 365 days
    vm.warp(block.timestamp + 365 days);
    msUSDDebt.accrueInterest();
    uint256 debtOnce = msUSDDebt.balanceOf(user); // == 100 * 1.5 = 150e18

    // Reset to identical state, then Path B: accrue daily
    for (uint256 i; i < 365; ++i) {
        vm.warp(block.timestamp + 1 days);
        msUSDDebt.accrueInterest(); // public, no auth
    }
    uint256 debtDaily = msUSDDebt.balanceOf(user); // ~100 * e^0.5 ≈ 164.9e18

    // Same stated APR, ~10% relative difference in borrower debt
    assertGt(debtDaily, debtOnce);
    // An EOA calling accrueInterest() every block forces debtDaily-style growth
    // on all borrowers regardless of the advertised 50% APR.
}
```

The same divergence is demonstrated by `test/DebtToken.test.ts` lines 816-830, where two sequential year-long accruals compound (100 → 110 → 165) rather than linearly (150).

### Citations

**File:** contracts/DebtToken.sol (L551-566)
```text
    function _calculateInterestAccrual()
        private
        view
        returns (uint256 _interestAmountAccrued, uint256 _debtIndex, uint256 _lastTimestampAccrued)
    {
        _lastTimestampAccrued = lastTimestampAccrued;
        _debtIndex = debtIndex;

        if (block.timestamp > _lastTimestampAccrued) {
            uint256 _interestRateToAccrue = interestRatePerSecond() * (block.timestamp - _lastTimestampAccrued);
            if (_interestRateToAccrue > 0) {
                _interestAmountAccrued = _interestRateToAccrue.wadMul(totalSupply_);
                _debtIndex += _interestRateToAccrue.wadMul(_debtIndex);
            }
        }
    }
```
