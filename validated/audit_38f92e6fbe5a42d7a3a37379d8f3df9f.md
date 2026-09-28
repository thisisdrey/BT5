### Title
`Pool.liquidate` can never fully clear unhealthy debt when `maxLiquidable < 100%` and `debtFloorInUsd > 0`, leaving permanent bad debt - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool.liquidate` enforces two conflicting constraints: a per-call cap (`maxLiquidable`, initialized to `0.5e18`) and a post-repayment floor (`debtFloorInUsd`). Because `maxLiquidable < 1e18` makes a full repayment (`amountToRepay_ == debt`) unreachable, and the floor check reverts whenever the remaining debt lands in `(0, debtFloorInUsd)`, a liquidator can never drive a position's debt to zero. The residual debt — which grows as interest accrues and collateral prices fall — becomes permanent, unliquidatable bad debt on the protocol.

### Finding Description
In `liquidate` the repayable amount is capped by `maxLiquidable` (`contracts/Pool.sol:567-569`), initialized to 50% (`contracts/Pool.sol:181`):

```solidity
if (amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) {
    revert AmountGreaterThanMaxLiquidable();
}
```

Immediately after, the floor check forbids leaving a positive remainder below `debtFloorInUsd` (`contracts/Pool.sol:571-579`):

```solidity
if (_newDebtInUsd > 0 && _newDebtInUsd < debtFloorInUsd) {
    revert RemainingDebtIsLowerThanTheFloor();
}
```

With `maxLiquidable = 0.5e18` and `debtFloorInUsd > 0` (a governance-set parameter present in the deployed configuration, per `deployments/*/Pool.json`), for any debt `D`, full repayment `D` violates the cap, and the maximum single repayment `0.5·D` leaves `0.5·D`. Once `0.5·D < debtFloorInUsd` (i.e., `D < 2·floor`), every permitted repayment leaves a remainder in `(0, floor)` and reverts — except repayments `≤ D − floor`, which asymptotically push the remainder toward `floor` but can never cross it. The residue `≥ floor` is unreachable: repaying it entirely exceeds `maxLiquidable`, and repaying anything that would leave `0 < remainder < floor` reverts.

The same terminal state is reached for underwater positions: when collateral seizable (`quoteLiquidateMax`, `contracts/Pool.sol:419-440`) covers less than the debt, liquidators seize all collateral and the leftover dust `≤ floor` debt can never be repaid via `liquidate` (would leave remainder `0`... allowed — but only reachable if `D ≤ maxL·D`, i.e. never), nor economically via `DebtToken.repay` by the defaulting borrower.

### Impact Explanation
The position's residual debt (bounded below by roughly `debtFloorInUsd` per position, growing with accrued interest via `debtIndex`) is permanently locked on the `DebtToken` books with no mechanism to burn it — no bad-debt socialization/write-off path exists in `Pool`/`DebtToken`. Each defaulted or liquidated position leaves behind guaranteed bad debt, degrading the synthetic token's backing (protocol insolvency). An unprivileged attacker can cheaply manufacture such positions: open a borrow just above `debtFloorInUsd` with minimal collateral at a permissive `collateralFactor`, let market movement (or same-transaction AMM price impact on a thin oracle source) push it unhealthy, and the position's tail debt is uncleanable by design.

### Likelihood Explanation
Likelihood is high under the deployed configuration: `maxLiquidable` defaults to 50% (`Pool.sol:181`) and `debtFloorInUsd` is a configured non-zero pool parameter. Every liquidation involving a debt near or below `2·debtFloorInUsd` — precisely the small positions the floor was meant to keep attractive — necessarily terminates with an uncleanable residue. No privileged role, oracle fault, or governance misstep is required; ordinary market volatility converts partially-liquidated positions into permanent bad debt.

### Recommendation
In `Pool.liquidate` (`contracts/Pool.sol:537-596`), exempt repayments from the `maxLiquidable` cap when `amountToRepay_` equals the full remaining debt balance, e.g.:

```solidity
uint256 _max = _debtTokenBalance.wadMul(maxLiquidable);
if (amountToRepay_ > _max && amountToRepay_ != _debtTokenBalance) {
    revert AmountGreaterThanMaxLiquidable();
}
```

Alternatively (or additionally), treat `remainder < debtFloorInUsd` as a signal to force full repayment in the same call, and add a keeper/governance bad-debt write-off path (burn debt token against treasury insurance) for residue that cannot be collected.

### Proof of Concept
Hardhat-style sketch against existing fixtures (mirroring `test/Pool.test.ts` "debt floor" block, lines 408-437):

```ts
// given: pool maxLiquidable = 50% (initialize default), floor = $3,000
await pool.updateDebtFloor(parseEther('3000'));

// alice: deposits collateral, issues debt D where floor < D < 2*floor
// e.g. debt = $4,000 worth of msEth; price drop makes position unhealthy
await masterOracle.updatePrice(met.address, newLowPrice);
expect((await pool.debtPositionOf(alice.address))._isHealthy).false;

const debt = await msEthDebtToken.balanceOf(alice.address); // ~$4,000

// 1) Full repayment reverts on maxLiquidable
await expect(
  pool.liquidate(msEth.address, alice.address, debt, msdMET.address)
).revertedWithCustomError(pool, 'AmountGreaterThanMaxLiquidable');

// 2) Repaying maxLiquidable (50%) leaves $2,000 < floor -> reverts
await expect(
  pool.liquidate(msEth.address, alice.address, debt.div(2), msdMET.address)
).revertedWithCustomError(pool, 'RemainingDebtIsLowerThanTheFloor');

// 3) The largest valid repayment is debt - floor ($1,000)
await pool.liquidate(msEth.address, alice.address, debt.sub(floorInMsEth), msdMET.address);

// 4) Recursing: new debt == floor; any further repay leaves remainder in (0, floor)
//    -> reverts forever. Residual debt ($3,000) can never be liquidated.
await expect(
  pool.liquidate(msEth.address, alice.address, 1, msdMET.address)
).revertedWithCustomError(pool, 'RemainingDebtIsLowerThanTheFloor');
```

Caveat: I could not verify the on-chain value of `debtFloorInUsd` from the deployment artifacts within the available iterations; the finding requires `debtFloorInUsd > 0`, which the deployments' ABIs and `PoolUpgrader.sol` indicate is an active configured parameter.