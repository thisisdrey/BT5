### Title
Unliquidatable dust debt: `debtFloorInUsd` combined with `maxLiquidable < 100%` permanently DoSes `Pool.liquidate` — ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool.liquidate` enforces two bounds on `amountToRepay_` that are mutually exclusive for any position whose total debt `D` is below `debtFloorInUsd` (or whose remainder would fall below it): (1) `amountToRepay_.wadDiv(D) <= maxLiquidable` and (2) remaining debt must be either `0` or `>= debtFloorInUsd`. When `maxLiquidable < 1e18` and `debtFloorInUsd > 0`, a position with `0 < debtInUsd < debtFloorInUsd` can never be liquidated — every call reverts. This mirrors the DoS class of CVE-2018-3174 (permanent denial of a core function).

### Finding Description
In `contracts/Pool.sol:567-579`:

```solidity
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

For a position with debt value `0 < V < debtFloorInUsd`:
- Any `amountToRepay_ < D` leaves `0 < _newDebtInUsd < debtFloorInUsd` → `RemainingDebtIsLowerThanTheFloor`.
- `amountToRepay_ == D` yields `wadDiv == 1e18 > maxLiquidable` → `AmountGreaterThanMaxLiquidable` (when `maxLiquidable < 1e18`).

The same wall is hit earlier: a position with `debtFloorInUsd <= V < debtFloorInUsd / (1 - maxLiquidable)` can only be partially liquidated down to exactly the floor, after which the remainder is permanently stuck. `quoteLiquidateMax` (Pool.sol:419-440) will happily return a value that then reverts inside `liquidate`, so even honest liquidators calling the protocol's own quote get a reverting transaction.

`DebtToken.mint`/`issue` do not enforce the floor at borrow time (the floor is only checked in `liquidate`), so an unprivileged user can create a sub-floor debt position directly.

### Impact Explanation
- **Permanent bad debt / liquidation liveness failure**: an underwater position whose debt sits below the floor can never be liquidated. The protocol must socialize the loss or hold it forever — direct protocol insolvency accrual.
- **Permanently frozen collateral**: the seized collateral is locked in `DepositToken` because `unlockedBalanceOf`/withdrawal paths require a healthy position, and no one can ever liquidate or repay on the user's behalf via `liquidate`.
- Attackers can mass-produce such positions (many accounts, minimal collateral, debt just under the floor), making the aggregate bad debt material while each position is individually unliquidatable.

### Likelihood Explanation
- No privileged role needed: any EOA deposits collateral, mints synthetic debt below `debtFloorInUsd`, and waits for interest accrual (`DebtToken.accrueInterest`, called inside `liquidate` at line 557, itself grows debt) or normal price drift to push the position below the collateral factor. Same-transaction oracle manipulation can also force it instantly.
- The only precondition is the deployed configuration `maxLiquidable < 1e18` and `debtFloorInUsd > 0` — both are governor-set parameters intended to be nonzero/`<100%` (the floor exists specifically to keep liquidation profitable, per PoolStorage.sol:21-25). Caveat: I could not confirm the exact on-chain values in `deploy/` scripts; the finding is conditional on this configuration, which is the documented intended one.

### Recommendation
In `liquidate`, bypass the `maxLiquidable` check when the repayment would zero out the debt, or automatically cap `amountToRepay_` and treat "repay the rest" as full close. E.g.:

```solidity
if (amountToRepay_ == _debtTokenBalance) {
    // full close: skip maxLiquidable and floor checks
} else {
    if (amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) revert AmountGreaterThanMaxLiquidable();
    ...floor check...
}
```

Alternatively, enforce `debtFloorInUsd` as a *minimum borrow* at `DebtToken.mint`/`issue` so sub-floor positions can never be created (still needed in `liquidate` since interest can push existing debt across boundaries).

### Proof of Concept
Hardhat fork test sketch:

```ts
// config: maxLiquidable = 0.5e18 (50%), debtFloorInUsd = $1,000
await pool.updateMaxLiquidable(parseEther('0.5'));
await pool.updateDebtFloor(parseEther('1000'));

// attacker: deposit collateral, mint ~$800 of msUSD (below floor)
await depositToken.connect(attacker).deposit(collateralAmount);
await msUsdDebtToken.connect(attacker).issue(mintAmount, attacker.address);

// force unhealthy: drop collateral price in oracle (or let interest accrue)
await masterOracle.updatePrice(collateral, lowPrice);
expect((await pool.debtPositionOf(attacker.address))._isHealthy).false;

// liquidator tries every option:
const debt = await msUsdDebtToken.balanceOf(attacker.address);
// partial repay → RemainingDebtIsLowerThanTheFloor
await expect(pool.liquidate(msUsd.address, attacker.address, debt.div(2), depositToken.address))
  .revertedWithCustomError(pool, 'RemainingDebtIsLowerThanTheFloor');
// full repay → AmountGreaterThanMaxLiquidable (1e18 > 0.5e18)
await expect(pool.liquidate(msUsd.address, attacker.address, debt, depositToken.address))
  .revertedWithCustomError(pool, 'AmountGreaterThanMaxLiquidable');
// quoteLiquidateMax also returns a reverting amount
const max = await pool.quoteLiquidateMax(msUsd.address, attacker.address, depositToken.address);
await expect(pool.liquidate(msUsd.address, attacker.address, max, depositToken.address))
  .revertedWithCustomError(pool, 'RemainingDebtIsLowerThanTheFloor');
// position is permanently unliquidatable: bad debt + locked collateral
```