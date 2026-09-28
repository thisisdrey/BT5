### Title
`maxLiquidable` close factor can be bypassed by repeated `liquidate()` calls, enabling full position liquidation - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
The external report (CWE-307) describes a protection mechanism (account lockout) that is defeated because the attempt counter/limit is enforced per-request rather than cumulatively — an attacker simply retries indefinitely. The same bug class exists in `Pool.liquidate()`: the `maxLiquidable` close-factor is enforced per call against the *current* (already-reduced) debt balance, so a liquidator can call `liquidate()` repeatedly — even in a single transaction — each time repaying up to `maxLiquidable` of the *remaining* debt, until virtually the entire position is liquidated and the maximum collateral is seized.

### Finding Description
`Pool.liquidate()` enforces the liquidation bound as a per-call ratio:

```solidity
// contracts/Pool.sol:565-569
uint256 _debtTokenBalance = _debtToken.balanceOf(account_);

if (amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) {
    revert AmountGreaterThanMaxLiquidable();
}
```

Because `_debtTokenBalance` is re-read on every call *after* the previous repayment, the cap only bounds each individual call, not the cumulative amount liquidated. With `maxLiquidable = 50%`, one call repays 50% of the debt; a second call repays 50% of the *new* balance (25% of original), and so on. Each call also passes the `PositionIsHealthy` gate as long as the position is still unhealthy — which is guaranteed while collateral is being stripped, since `quoteLiquidateOut` only checks that `_totalSeized <= depositToken_.balanceOf(account_)` (`Pool.sol:583`), and the remaining debt/collateral ratio keeps the position underwater.

Two paths let the attacker drain essentially all seizable collateral:

- **Loop until `AmountIsTooHigh`**: repeat `liquidate(synth, victim, debtBalance.wadMul(maxLiquidable), collateral)`; the loop only stops when `_totalSeized` exceeds the victim's deposit balance, i.e. when nearly all collateral has been seized.
- **Cross-collateral termination**: if multiple `DepositToken`s exist, repeat per collateral token.

The `debtFloorInUsd` check (`Pool.sol:571-579`) is not a mitigation: it only prevents the residual debt from landing in `(0, floor)`; the attacker either leaves a residual ≥ floor or, if the remaining debt is already below the floor, stops at the last valid step — in both cases the collateral seizure has already happened.

The meta-sender path does not block it either: `_msgSender()` is only checked against `CanNotLiquidateOwnPosition`, and a batching contract via `Operator.execute`/multicall inherits the same per-call semantics.

### Impact Explanation
The `maxLiquidable` close factor exists to bound how much of a position can be liquidated in one action, limiting borrower losses to the incentive applied on that fraction. Because the bound is cumulative-in-name-only, the borrower loses incentive (`_toLiquidator = repaid * (1 + liquidatorIncentive)`, `Pool.sol:406-408, 462-469`) on up to ~100% of their debt instead of `maxLiquidable` of it, and loses collateral far beyond what the configured bound intended. The invariant that breaks is the liquidation bound — the deployed protection is fully defeated by trivial repetition, exactly as the Liferay lockout is defeated by repeated login attempts. The loss is direct loss of user collateral to the liquidator.

### Likelihood Explanation
Requires only an unhealthy victim position (a normal, recurring state) and an unprivileged liquidator holding the synthetic token — obtainable via `DebtToken.issue`/`mint` with own collateral or via `Pool.swap`. No governor/keeper/oracle involvement. If `maxLiquidable < 1e18` is configured on any deployment (the tests exercise `0.5e18`), the exploit is a simple loop, executable atomically via a contract in one transaction. Cost is only gas plus the synth needed for repayment, which is repaid-debt denominated and fully recovered through seized collateral plus incentive.

### Recommendation
Enforce the close factor on a cumulative basis rather than per-call, e.g.:

- Track liquidated amount per account within a time window or per unhealthy episode and cap the cumulative repaid amount at `maxLiquidable * initialDebt`, resetting only when the position returns to health.
- Alternatively, snapshot the debt balance at the start of a liquidation episode (e.g., on first unhealthy liquidation) and require `cumulativeRepaid <= initialDebt.wadMul(maxLiquidable)` before further liquidation is allowed.

### Proof of Concept
Hardhat fork sketch (assumes deployed pool with `maxLiquidable = 0.5e18`, an underwater `victim` position with `msdX` collateral and `msY` debt, and the attacker holding ≥ debt worth of `msY`):

```solidity
function test_closeFactorBypass() public {
    // victim is unhealthy
    (bool healthy,,,,) = pool.debtPositionOf(victim);
    assertFalse(healthy);

    uint256 collateralBefore = msdX.balanceOf(victim);
    uint256 maxLiquidable = pool.maxLiquidable(); // e.g. 50%

    // Loop: each call repays <= 50% of *current* debt
    for (uint i = 0; i < 20; i++) {
        uint256 debt = msYDebt.balanceOf(victim);
        uint256 amount = debt.wadMul(maxLiquidable);
        if (amount == 0) break;
        try pool.liquidate(msY, victim, amount, msdX) {
        } catch {
            // stops on AmountIsTooHigh (collateral exhausted)
            // or RemainingDebtIsLowerThanTheFloor (debt dust left)
            break;
        }
    }

    // Collateral seized far exceeds what a single 50% liquidation would take
    uint256 seized = collateralBefore - msdX.balanceOf(victim);
    assertGt(seized, collateralBefore / 2); // >> close factor bound
}
```

Reproducible against the repo's own test harness: the existing tests (`test/Pool.test.ts:439-451`) only assert that a *single* call exceeding `maxLiquidable` reverts; no test asserts a cumulative bound, confirming the gap.

*Note: the configured on-chain `maxLiquidable` value per deployment could not be confirmed from the indexed deployment artifacts; the finding requires `maxLiquidable < 1e18` on the target pool, which the codebase clearly supports and intends (the `MaxLiquidableTooHigh` guard exists precisely because values below 100% are meaningful).*