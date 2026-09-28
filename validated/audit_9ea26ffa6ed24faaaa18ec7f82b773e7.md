### Title
Bad debt can never be cleared when the max liquidatable repay leaves residual debt below `debtFloorInUsd` - (File: contracts/Pool.sol)

### Summary
`Pool.liquidate` reverts with `RemainingDebtIsLowerThanTheFloor` whenever the post-liquidation debt is non-zero and below `debtFloorInUsd`. For an underwater account (debt > collateral), the maximum repay a liquidator can make is capped by the account's collateral balance via `AmountIsTooHigh`. If that max repay leaves residual debt under the floor, every liquidation attempt reverts, so the bad debt is permanently stuck — the same bug class as "a threshold check prevents clearing the maximum bad debt possible."

### Finding Description
In `Pool.liquidate` (contracts/Pool.sol:534-596):

- `amountToRepay_` is bounded above by `maxLiquidable` (line 567) and implicitly by collateral: `quoteLiquidateOut` computes `_totalSeized`, and line 583 reverts `AmountIsTooHigh` if it exceeds `depositToken_.balanceOf(account_)`.
- Separately, when `debtFloorInUsd > 0`, lines 571-579 revert `RemainingDebtIsLowerThanTheFloor` if the remaining debt `_debtTokenBalance - amountToRepay_` is `> 0` and `< debtFloorInUsd`.

For an underwater position, the liquidator's best move is `quoteLiquidateMax` (contracts/Pool.sol:419-440), which returns `min(balance-covering repay, debt * maxLiquidable)`. When collateral is exhausted, the residual debt `debt - amountToRepay` is still positive. If that residual is worth less than `debtFloorInUsd` (denominated in USD, e.g. a $100 floor vs. a few dollars of dust bad debt — a very common outcome for underwater positions), the liquidator cannot:

- repay more (seize would exceed balance → `AmountIsTooHigh`),
- repay less (residual still under the floor → `RemainingDebtIsLowerThanTheFloor`).

The liquidation is permanently bricked; no attacker action or privileged role is required — the state is reachable by any account whose collateral value drops below its debt (oracle price move or accrued interest via `DebtToken.accrueInterest`).

### Impact Explanation
Bad debt (debt backed by zero collateral) remains on the books indefinitely. The protocol keeps counting msAsset debt that can never be repaid or liquidated, meaning circulating synthetic supply is partially unbacked — protocol insolvency that grows with interest accrual. This mirrors the referenced finding: a conditional check meant as a guard instead prevents clearing the maximum amount of debt possible.

### Likelihood Explanation
Requires an underwater position whose leftover debt after seizing all collateral is below `debtFloorInUsd`. Since the floor is a fixed USD amount, any position whose collateral deficit is small in USD terms hits this deterministically — no edge-case timing needed. Common for small accounts liquidated late or after sharp collateral price drops.

### Recommendation
Skip the floor check when the position is undercollateralized / when repaying the maximum possible amount, e.g. only enforce `RemainingDebtIsLowerThanTheFloor` if `amountToRepay_` is below the amount needed to seize the full collateral balance, or waive the floor when `debtPositionOf` shows collateral is exhausted.

### Proof of Concept
Hardhat, mirroring the existing "unhealthy (collateral:debt < 1)" suite in test/Pool.test.ts:773+:

```ts
// given: alice underwater, debtFloorInUsd > residual debt
await masterOracle.updatePrice(met.address, toUSD('0.50')) // collateral < debt
await pool.updateDebtFloorInUsd(parseEther('100'))          // $100 floor

// residual debt after seizing all collateral < $100
const amountToRepay = await pool.quoteLiquidateMax(msEth.address, alice.address, msdMET.address)

// when / then: liquidation permanently reverts
await expect(
  pool.connect(liquidator).liquidate(msEth.address, alice.address, amountToRepay, msdMET.address)
).revertedWithCustomError(pool, 'RemainingDebtIsLowerThanTheFloor')
// bad debt can never be cleared
```