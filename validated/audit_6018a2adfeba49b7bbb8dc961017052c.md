### Title
Liquidations below the incentive threshold push borrowers into insolvency, creating unbacked bad debt — ([File: contracts/Pool.sol])

### Summary
`Pool.liquidate` always seizes collateral worth `amountToRepay * (1 + liquidatorIncentive + protocolFee)` (default 18% total) regardless of the borrower's solvency ratio. When a position's debt exceeds `collateral / (1 + totalFees)` (~84.7% of raw collateral value with defaults), each liquidation removes more collateral value than the debt it burns, so the liquidation itself drives the borrower toward (or across) insolvency and leaves residual unbacked debt in the protocol — the same "counterproductive incentives" class described in the report.

### Finding Description
In `quoteLiquidateOut`, collateral seized is `repayValue * (1 + _liquidatorIncentive) + _fee`, i.e., the liquidator receives collateral at better-than-market rates and the protocol takes a further cut (`contracts/Pool.sol:451-472`). `liquidate` only enforces: position unhealthy, `amountToRepay <= debt * maxLiquidable`, remaining debt above `debtFloorInUsd`, and `_totalSeized <= depositToken_.balanceOf(account_)` (`contracts/Pool.sol:559-585`). There is no check that the liquidation actually improves the borrower's solvency ratio.

Default fees from `FeeProvider.initialize` are `liquidatorIncentive = 10%` and `protocolFee = 8%` (`contracts/FeeProvider.sol:66-69`). The solvency breakeven is therefore `D/C = 1/1.18 ≈ 0.847`. For positions with `D/C` above this (but still `C > D`, i.e., undercollateralized rather than insolvent), a liquidator repaying `x` reduces the borrower's surplus `C - D` by `0.18 * x`; a sufficiently large `x` (bounded only by `maxLiquidable`) turns the position insolvent outright. The protocol's own tests confirm this end state: in `test/Pool.test.ts` "when the position is unhealthy (collateral:debt < 1)", liquidation seizes essentially the entire deposit while `debtToken.balanceOf(alice) > 0` remains as pure bad debt.

### Impact Explanation
Protocol insolvency: debt tokens with no backing collateral remain in the system, accruing interest (`DebtToken.accrueInterest`), while the borrower's collateral has been fully drained to the liquidator and feeCollector. The borrower has no incentive to repay the residual debt, so it is permanently unbacked — the loss is socialized across all holders of that synthetic asset, which can no longer be redeemed 1:1 against pool collateral.

### Likelihood Explanation
- Reachable by any unprivileged EOA via `Pool.liquidate` with only minted synths (which can be flash-minted via `DebtToken.flashIssue` or bought on-market).
- Requires a position to drift past the 84.7% raw collateral-value threshold before liquidators act — plausible during sharp price moves, oracle lag, or for collaterals with high collateral factors where the health boundary sits close to insolvency.
- Any governor-set fees within `MAX_FEE_VALUE` (25%) exhibit the same behavior; the mechanism is structural, not parameter-dependent.

### Recommendation
- Make the effective incentive dynamic: cap the total seized so liquidation can never push a position below solvency, e.g., limit `_totalToSeize` such that post-liquidation `C' / D'` does not decrease.
- Alternatively, treat the `1/(1+totalFees)` threshold as insolvency for liquidation purposes: allow liquidators to repay the full position (and seize all collateral) rather than perpetuating the incentive-drain trajectory.
- At minimum, document and account for the guaranteed incentive spend as protocol bad-debt exposure when setting `collateralFactor` and `maxLiquidable`.

### Proof of Concept
Hardhat (pattern matches the existing `Pool.test.ts` unhealthy-position suite):

```ts
// Setup: alice deposits MET collateral (msdMET), mints max msETH via msEthDebtToken.issue().
// Crash MET price so that debtInUsd > depositInUsd / 1.18 but debtInUsd < depositInUsd
// (undercollateralized yet still above-water).
await masterOracle.updatePrice(met.address, newMetPrice)

// Liquidator repays the max allowed amount.
const maxRepay = await pool.quoteLiquidateMax(msEth.address, alice.address, msdMET.address)
await pool.connect(liquidator).liquidate(msEth.address, alice.address, maxRepay, msdMET.address)

// Assert: borrower's deficit grew.
// debtInUsdAfter - collateralInUsdAfter > debtInUsdBefore - collateralInUsdBefore
// With maxLiquidable-sized repay and C/D close to 1, the position becomes insolvent:
//   msdMET.balanceOf(alice) ~ 0  AND  msEthDebtToken.balanceOf(alice) > 0
const debtAfter = await pool.debtOf(alice.address)
const depositAfter = await msdMET.balanceOf(alice.address)
expect(await msEthDebtToken.balanceOf(alice.address)).gt(0)
expect(depositAfter).lt(depositBefore.sub(seizedValueInToken))
// Surplus check: 0.18 * repaidValue > collateral surplus => insolvency created by the liquidation itself.
```

The existing test at `test/Pool.test.ts:791-819` already demonstrates the terminal state: near-total collateral seized with residual debt remaining — i.e., liquidation in this regime manufactures permanent unbacked debt rather than restoring solvency.