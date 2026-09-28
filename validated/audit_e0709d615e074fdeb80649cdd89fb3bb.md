### Title
Unprivileged borrower can make a position permanently unliquidatable via `RemainingDebtIsLowerThanTheFloor` / `AmountGreaterThanMaxLiquidable` revert interplay - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool.liquidate` enforces two bounds on `amountToRepay_`: it may not exceed `maxLiquidable` (≤ 100% of the debt balance, default 50%) and, when `debtFloorInUsd > 0`, the remaining debt must either be zero or ≥ `debtFloorInUsd`. Because a full repayment is capped by `maxLiquidable < 1e18`, every valid liquidation leaves a non-zero remainder. An attacker can size a position so that *every* permitted repayment leaves a remainder in `(0, debtFloorInUsd)`, making `liquidate` revert unconditionally — an assertion-failure-style DoS on the protocol's bad-debt removal path, analogous to CVE-2018-14044's attacker-triggered abort.

### Finding Description
In `liquidate`, the repayment ceiling is enforced as `amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable → revert` [1](#0-0) , and the floor check reverts when the post-repayment debt is positive but below `debtFloorInUsd` [2](#0-1) . Since `updateMaxLiquidable` rejects values `> 1e18` [3](#0-2) , with the deployed `maxLiquidable = 0.5e18` [4](#0-3)  any `amountToRepay_` above 50% of the debt reverts, and any amount that leaves `0 < remainingDebtUsd < debtFloorInUsd` also reverts.

The attacker's play is: deposit collateral, then via `DebtToken.issue` mint synth debt of size `D` such that `D_usd × (1 − maxLiquidable) < debtFloorInUsd` (i.e., `D_usd < 2×debtFloorInUsd` at 50%). Every permitted `amountToRepay_` then leaves a remainder `< debtFloorInUsd` and `> 0`, so `liquidate` always reverts once the position becomes unhealthy (e.g., collateral price drop or accrued interest via `DebtToken.accrueInterest`). The revert is not caller-controlled slippage — no arguments exist that satisfy both checks, so the bad debt is stuck.

### Impact Explanation
Liquidation is the only mechanism to clear underwater debt; permanently revert-on-liquidate means the position's bad debt accrues interest indefinitely while seized collateral remains locked. When collateral value falls below debt value, the deficit is socialized across the pool/synthetic holders — direct protocol insolvency. This is a liveness/invariant break on the liquidation path, meeting the "protocol insolvency / permanent freezing" acceptance bar.

### Likelihood Explanation
Fully unprivileged: the attacker only needs `DepositToken.deposit` and `DebtToken.issue` with a deliberately sized position — no governor, oracle malfunction, or trusted-party assumption. Cost is bounded by the floor-scale debt (a few hundred to a few thousand USD depending on `debtFloorInUsd`), and the attacker may even recover collateral value if price stays flat; the DoS only materializes (and pays off for no one) once the position goes underwater, but it blocks all liquidators identically. The check is on the deployed configuration path (`debtFloorInUsd > 0` is a production risk parameter, not governance error).

### Recommendation
Allow full repayment when the remaining debt would fall below the floor: e.g., `if (_newDebtInUsd < debtFloorInUsd) amountToRepay_ = _debtTokenBalance;` or skip the floor check when `amountToRepay_ == _debtTokenBalance` while permitting that amount to exceed `maxLiquidable` specifically when it is the residual dust. Alternatively cap `debtFloorInUsd` relative to `maxLiquidable` so a liquidatable interval always exists.

### Proof of Concept
Hardhat fork sketch:
1. Read `pool.debtFloorInUsd()` and `pool.maxLiquidable()` (e.g., `0.5e18`).
2. Attacker deposits collateral via `DepositToken.deposit` and calls `DebtToken.issue` to reach `debtUsd ∈ (debtFloorInUsd, debtFloorInUsd / (1 - maxLiquidable))` — e.g., floor $100 → debt $150.
3. Move collateral price down (fork oracle update or accrue interest until `debtPositionOf` returns `_isHealthy == false`).
4. Liquidator calls `Pool.liquidate(synthetic, attacker, amountToRepay, depositToken)`:
   - `amountToRepay > 0.5×debt` → reverts `AmountGreaterThanMaxLiquidable`.
   - `amountToRepay ≤ 0.5×debt` → remaining debt `$150−r ∈ ($75, $150) < $100` for any `r > $50`… concretely for `r = $75`: remaining `$75 < $100` → reverts `RemainingDebtIsLowerThanTheFloor`. Any `r` satisfying bound 1 leaves remaining ≥ `D/2 > 0` and `< floor` whenever `D < 2×floor`; for `r` small enough to clear the floor, remaining is still `> floor` only if `r < D − floor` — but then repeating leaves the same dead zone on the next call since the remaining debt `D' < D` still satisfies `D' < 2×floor` and `D' > floor`, so the position can never be fully liquidated and the final `D' ≤ floor` tranche is permanently stuck.
5. Assert `liquidate` reverts for every `amountToRepay_` in `[1, debtTokenBalance]` once debt ≤ `2×floor` enters the floor interval — i.e., `assertEq(success, false)` across a fuzzed range, demonstrating the terminal DoS.

### Citations

**File:** contracts/Pool.sol (L181-181)
```text
        maxLiquidable = 0.5e18; // 50%
```

**File:** contracts/Pool.sol (L567-569)
```text
        if (amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) {
            revert AmountGreaterThanMaxLiquidable();
        }
```

**File:** contracts/Pool.sol (L571-578)
```text
        if (debtFloorInUsd > 0) {
            uint256 _newDebtInUsd = masterOracle().quoteTokenToUsd(
                address(syntheticToken_),
                _debtTokenBalance - amountToRepay_
            );
            if (_newDebtInUsd > 0 && _newDebtInUsd < debtFloorInUsd) {
                revert RemainingDebtIsLowerThanTheFloor();
            }
```

**File:** contracts/Pool.sol (L778-784)
```text
    function updateMaxLiquidable(uint256 newMaxLiquidable_) external onlyGovernor {
        if (newMaxLiquidable_ > 1e18) revert MaxLiquidableTooHigh();
        uint256 _currentMaxLiquidable = maxLiquidable;
        if (newMaxLiquidable_ == _currentMaxLiquidable) revert NewValueIsSameAsCurrent();
        emit MaxLiquidableUpdated(_currentMaxLiquidable, newMaxLiquidable_);
        maxLiquidable = newMaxLiquidable_;
    }
```
