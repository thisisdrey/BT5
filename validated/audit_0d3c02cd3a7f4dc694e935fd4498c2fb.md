### Title
Positions whose debt is at (or near) `debtFloorInUsd` cannot be liquidated — `Pool.liquidate` permanently reverts (contracts/Pool.sol)

### Summary
`Pool.liquidate` enforces two constraints that contradict each other: the repayment amount is capped by `maxLiquidable` (50% of the debt balance on the deployed configuration), and any partial repayment that leaves a residual debt in `(0, debtFloorInUsd)` reverts with `RemainingDebtIsLowerThanTheFloor`. When an account's debt is small enough that `debtBalance * (1 - maxLiquidable) < debtFloorInUsd` while `debtBalance <= debtFloorInUsd`, no valid `amountToRepay_` exists and liquidation always reverts, matching the CVE-2018-1000615 bug class (unprivileged remote actor making a critical protocol function crash/revert for a reachable state).

### Finding Description
`liquidate` computes the allowed repayment bounds:

```solidity
if (amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) {
    revert AmountGreaterThanMaxLiquidable();            // caps repay at 50%
}

if (debtFloorInUsd > 0) {
    uint256 _newDebtInUsd = masterOracle().quoteTokenToUsd(
        address(syntheticToken_),
        _debtTokenBalance - amountToRepay_
    );
    if (_newDebtInUsd > 0 && _newDebtInUsd < debtFloorInUsd) {
        revert RemainingDebtIsLowerThanTheFloor();
    }
}
``` [1](#0-0) 

For a position with debt `D`, the liquidator may only repay `a <= D * maxLiquidable`. The floor check additionally requires `D - a` to be either `0` or `>= debtFloorInUsd`. Full repayment (`a = D`, remaining = 0) is impossible whenever `maxLiquidable < 1e18`. Therefore, when `D <= debtFloorInUsd`, every permitted repayment leaves a residual strictly between `0` and `debtFloorInUsd` and the call always reverts.

A borrower can trivially reach this state by issuing debt equal to the floor (issue paths enforce the floor on new debt, so `D >= debtFloorInUsd` at creation), making `D = debtFloorInUsd` an allowed, reachable state. An attacker can deliberately park such a position; once the collateral price drops and the position becomes unhealthy, `liquidate` is unreachable for it.

### Impact Explanation
Liveness invariant broken: `Pool.liquidate` is permanently disabled for a whole class of reachable positions. An unhealthy position at the debt floor cannot be liquidated by anyone — each call reverts either with `AmountGreaterThanMaxLiquidable` or `RemainingDebtIsLowerThanTheFloor`. If the collateral continues to decline, the position accrues bad debt that cannot be cleared, leading to protocol insolvency borne by other depositors. Until accrued interest pushes `D` above `debtFloorInUsd / 1` enough that `D - debtFloorInUsd` repayments are possible, no liquidator can act — a temporary-to-permanent freeze of the liquidation function for that account, reachable with purely unprivileged calls (`DebtToken.issue` at the floor amount).

### Likelihood Explanation
High feasibility, conditional impact. The attacker only needs collateral worth slightly more than `debtFloorInUsd / collateralFactor` — a small amount if the floor is modest. No privileged role, oracle manipulation, or governance action is required; `issue` and `liquidate` are public entry points reachable directly or via `Operator.execute`. The impact materializes when the position turns unhealthy, which requires collateral price movement (normal market risk, not manipulated oracle data). Caveat: severity depends on `debtFloorInUsd > 0` being configured on the deployed pools (a governor-set value); if the floor is 0 this path is not reachable, and interest accrual can eventually push `D` above the floor threshold, narrowing the unliquidatable window.

### Recommendation
In `Pool.liquidate`, skip or relax the floor check when the repayment is capped by `maxLiquidable`, e.g., allow `RemainingDebtIsLowerThanTheFloor` only when a strictly larger repayment was permitted, or let liquidators fully repay (`amountToRepay_ == _debtTokenBalance`) when `D <= debtFloorInUsd / (1 - maxLiquidable)`. Alternatively, exempt repayments that bring the account's total debt to zero or treat `maxLiquidable` as `1e18` whenever the floor would otherwise make the position unliquidatable.

### Proof of Concept
Foundry fork sketch:

```solidity
function test_liquidate_floorDoS() public {
    // pool, depositToken, debtToken, msUSD configured; debtFloorInUsd = F > 0; maxLiquidable = 0.5e18
    uint256 floor = pool.debtFloorInUsd();

    // attacker deposits collateral and issues debt == floor
    uint256 collateralUsd = floor * 2 / collateralFactor; // healthy headroom
    depositToken.deposit(collateralAmount, attacker);
    debtToken.issue(msUsdAmountForUsd(floor), attacker);   // D == floor

    // collateral price drops via legitimate market move; position unhealthy
    oracle.setPrice(underlying, lowerPrice);

    // any liquidation attempt reverts:
    vm.expectRevert(Pool.AmountGreaterThanMaxLiquidable.selector);
    pool.liquidate(msUSD, attacker, debtToken.balanceOf(attacker), depositToken); // full repay blocked

    uint256 half = debtToken.balanceOf(attacker) / 2;
    vm.expectRevert(Pool.RemainingDebtIsLowerThanTheFloor.selector);
    pool.liquidate(msUSD, attacker, half, depositToken); // partial repay leaves < floor
}
```

Uncertainty notes: I verified the conflicting checks in `Pool.liquidate` directly, but did not inspect `DebtToken.issue`'s exact floor enforcement or the deployed `debtFloorInUsd`/`maxLiquidable` values; the finding is void if the floor is 0 on all deployed pools or if issue enforces `D > floor / (1 - maxLiquidable)`.

### Citations

**File:** contracts/Pool.sol (L567-579)
```text
        if (amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) {
            revert AmountGreaterThanMaxLiquidable();
        }

        if (debtFloorInUsd > 0) {
            uint256 _newDebtInUsd = masterOracle().quoteTokenToUsd(
                address(syntheticToken_),
                _debtTokenBalance - amountToRepay_
            );
            if (_newDebtInUsd > 0 && _newDebtInUsd < debtFloorInUsd) {
                revert RemainingDebtIsLowerThanTheFloor();
            }
        }
```
