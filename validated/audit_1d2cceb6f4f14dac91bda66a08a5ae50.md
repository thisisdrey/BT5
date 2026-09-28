### Title
Debt floor combined with `maxLiquidable` cap permanently blocks liquidation of small positions - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool.liquidate` enforces two independent constraints that contradict each other for a whole band of debt sizes: `maxLiquidable` caps a single repayment to a fraction of total debt (deployed default `0.5e18` = 50%), and `debtFloorInUsd` forces any non-zero remaining debt to be >= the floor. When a position's debt `D` satisfies `floor < D * maxLiquidable` scenario — specifically when `D - maxLiquidable*D < debtFloorInUsd` while `D > maxLiquidable*D` — every allowed repayment either leaves a remainder below the floor (revert `RemainingDebtIsLowerThanTheFloor`) or exceeds `maxLiquidable` (revert `AmountGreaterThanMaxLiquidable`). Full liquidation requires repaying 100%, which is impossible when `maxLiquidable < 1e18`. Such positions are permanently unliquidatable, producing guaranteed bad debt — the exact analog of forcing huge-page alignment on a constrained (32-bit) address space: a fixed minimum granularity that makes an entire size class unworkable.

### Finding Description
In `Pool.liquidate`:

- Line 567: `if (amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) revert AmountGreaterThanMaxLiquidable();` — caps `amountToRepay_ <= balance * maxLiquidable`.
- Lines 571–579: if `debtFloorInUsd > 0`, the remaining debt `_debtTokenBalance - amountToRepay_` priced in USD must be `0` or `>= debtFloorInUsd`, else revert.

With `maxLiquidable = 0.5e18` (set in `initialize`, line 181) and `debtFloorInUsd > 0`, the only way to leave a valid remainder is `amountToRepay_ == _debtTokenBalance` (remainder 0) — but that violates the 50% cap. Any repay `<= 50%` leaves `>= 50%` of debt. Therefore any position whose USD debt after a 50% repay would fall below the floor — i.e. `debtInUsd < 2 * debtFloorInUsd` — can never be liquidated. The floor check contains no exception for "repay the entire debt" because that path is already blocked by the cap.

Note the check measures the whole `DebtToken` balance for that synthetic (`_debtTokenBalance`), not the pool-wide debt, so this bands every account whose per-synth debt is in `(0, ~2*floor]`.

### Impact Explanation
An attacker (or any user) can open a position with debt deliberately sized in the unliquidatable band (e.g. debt USD value between `debtFloorInUsd` and `debtFloorInUsd / maxLiquidable`). Once the position goes underwater (collateral price drop, or debt appreciation via interest accrual in `DebtToken.accrueInterest`), no liquidator can ever repay any amount: small repays leave sub-floor debt, full repays exceed the cap. The position accrues bad debt indefinitely; the collateral cannot be seized, and the protocol's synth supply becomes undercollateralized — protocol insolvency, a qualifying impact.

### Likelihood Explanation
No privileged role is needed: `DebtToken.issue`/`Pool` deposit are public entry points, and the attacker chooses the debt size at issuance. The only requirement is that the deployed configuration has `debtFloorInUsd > 0` and `maxLiquidable < 1e18` — both true of the initialized defaults (`maxLiquidable = 0.5e18`) and of any deployment using a non-zero debt floor (the feature's stated purpose, per `PoolStorageV1.debtFloorInUsd` comments, is to keep liquidations profitable). Interest accrual alone can push a borderline position over the health threshold without any oracle manipulation. The condition is deterministic once reached — it is a permanent freeze of the account's collateral and a permanent bad-debt slot, not a transient state.

### Recommendation
In `Pool.liquidate`, when the repayment would clear the debt (or when `remainingDebtInUsd < debtFloorInUsd`), either:
- allow `amountToRepay_` up to the full debt balance (i.e., skip or relax the `maxLiquidable` cap when repaying 100%), or
- treat "repay would leave sub-floor remainder" as "must repay all" and clamp `amountToRepay_` to `_debtTokenBalance`, bypassing the cap for that case.

Concretely: compute the max repayable first; if `remaining < floor` and `remaining > 0`, require `amountToRepay_ == _debtTokenBalance` and exempt that path from `AmountGreaterThanMaxLiquidable`.

### Proof of Concept
Hardhat-style sketch against deployed-like config (`maxLiquidable = 0.5e18`, `debtFloorInUsd = $100`):

```ts
// Setup: pool with debtFloor = $100, collateral = MET, synth = msUSD
// 1. Attacker deposits collateral and issues debt worth $150 (band: $100..$200)
await msdMET.deposit(collateralAmount, attacker.address);
await msUSDDebt.issue(parseEther('150'), attacker.address); // ~$150 debt

// 2. Position becomes unhealthy (oracle price drop of MET, or interest accrual)
await masterOracle.updatePrice(met.address, lowerPrice); // fork/test price move
expect((await pool.debtPositionOf(attacker.address))._isHealthy).to.be.false;

// 3. Liquidator tries partial repay within cap: repay 50% => remaining ~$75 < floor
await expect(
  pool.connect(liq).liquidate(msUSD.address, attacker.address, parseEther('75'), msdMET.address)
).to.be.revertedWithCustomError(pool, 'RemainingDebtIsLowerThanTheFloor');

// 4. Liquidator tries full repay to leave 0 remainder => exceeds 50% cap
await expect(
  pool.connect(liq).liquidate(msUSD.address, attacker.address, parseEther('150'), msdMET.address)
).to.be.revertedWithCustomError(pool, 'AmountGreaterThanMaxLiquidable');

// 5. No valid amountToRepay exists: position is permanently unliquidatable,
//    collateral frozen in the account, bad debt accrues interest forever.
```

Caveat: I could not fully verify the exact `debtFloorInUsd` values set in the deployment scripts within this scan's budget; the vulnerability requires `debtFloorInUsd > 0` on the deployed configuration, which is the parameter's intended operating mode.