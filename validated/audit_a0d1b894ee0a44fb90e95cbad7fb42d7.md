### Title
Underwater positions with debt below `debtFloorInUsd` are permanently unliquidatable — bad debt accrual DoS - (File: contracts/Pool.sol)

### Summary
`Pool.liquidate` enforces two invariants that can never be satisfied simultaneously for an underwater position whose remaining debt value is below `debtFloorInUsd`: any partial repayment leaves debt in `(0, floor)` and reverts with `RemainingDebtIsLowerThanTheFloor`, while a full repayment seizes more collateral than the account holds and reverts with `AmountIsTooHigh`. The result is a position no unprivileged actor can ever liquidate or repay on behalf of — a liveness failure on the liquidation path analogous to a crash-inducing crafted-input DoS.

### Finding Description
In `Pool.liquidate` (contracts/Pool.sol:537-596):

1. `amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable` caps the repay fraction, so a liquidator may not always be able to repay in full even if they want to (line 567).
2. If `debtFloorInUsd > 0`, repaying an amount that leaves `0 < remainingDebtInUsd < debtFloorInUsd` reverts (lines 571-578).
3. `quoteLiquidateOut` computes `_totalSeized = collateralValue(amountToRepay) * (1 + liquidatorIncentive) + fee` (lines 451-471); when the position is underwater, `_totalSeized > depositToken_.balanceOf(account_)` for the full debt and the call reverts with `AmountIsTooHigh` (line 583).

Because collateral value per unit of debt includes a liquidation incentive markup, there is a price band where the position is unhealthy but `collateralValue(debt) * (1 + incentive) > collateralBalance`. Inside that band every `amountToRepay` either leaves sub-floor dust or over-seizes — the function reverts for all inputs.

The same trap exists on the voluntary side: `DebtToken.repay` applies the identical floor check (contracts/DebtToken.sol:442-450), so even a third party repaying on behalf of the position must repay in full (`repayAll`), which does not exist for liquidation and requires the payer to hold the full synth amount — while the protocol still cannot recover the collateral deficit.

Attacker path (no privileged role needed): deposit collateral, `DebtToken.issue` a debt just above `debtFloorInUsd` (mint enforces `balance + amount >= floor`, line 583-588), then wait for or induce via AMM a collateral price drop until the position is underwater. From that point `liquidate` reverts for every `amountToRepay_`, and the bad debt accrues interest indefinitely. The attacker can also manufacture the condition deterministically by repaying to just above the floor and letting interest/price drift push USD value below it — `_burn`/`repay` only check the *new* debt against the floor, so a debt that was ≥ floor at repay time can cross below it via price movement, with no path back.

### Impact Explanation
Permanent freezing of the liquidation mechanism for affected positions → protocol insolvency. Underwater debt that can never be liquidated or repaid accrues interest forever; the collateral backing other synths is effectively drained. Each such position is direct bad debt on the pool. Not dependent on oracle misbehavior — only on ordinary price movement, which the attacker can time.

### Likelihood Explanation
Requires `debtFloorInUsd > 0` (a deployed governance setting; the feature is live code, and tests exercise `updateDebtFloor`), a liquidation incentive `> 0`, and an underwater position with debt USD value < floor. On volatile collateral these conditions coincide naturally; an attacker can also deliberately open minimum-size positions at max leverage to harvest the state. Cost to attacker is only the (already lost) collateral.

### Recommendation
Skip the floor check when the repayment closes the position to the maximum extent collateral allows — e.g., in `liquidate`, treat "repay up to what the collateral covers" as a full close: cap `amountToRepay_` by `quoteLiquidateMax` semantics before applying the floor check, or exempt liquidations from `RemainingDebtIsLowerThanTheFloor` when `amountToRepay_` seizes the entire deposit balance. Alternatively allow liquidators to seize all collateral while repaying a proportional amount and forgive the residual dust debt.

### Proof of Concept
Hardhat fork outline (mirroring `test/Pool.test.ts` liquidation suite):

```ts
// 1. Governor config assumed live: debtFloorInUsd = F > 0, liquidatorIncentive > 0.
await pool.updateDebtFloor(F);

// 2. Attacker deposits collateral C and issues debt D with usdValue(D) >= F
await depositToken.deposit(C);
await debtToken.issue(D, attacker.address); // passes DebtLowerThanTheFloor check

// 3. Collateral price drops so position is underwater:
//    usdValue(D_remaining_band) region where
//    quote(collateral) < quote(D) * (1 + incentive)
await masterOracle.updatePrice(collateral.address, lowerPrice);

// 4. Any liquidation attempt reverts:
//    partial: revert RemainingDebtIsLowerThanTheFloor
//    full:    revert AmountIsTooHigh  (_totalSeized > balanceOf(attacker))
await expect(
  pool.liquidate(msEth.address, attacker.address, partialAmount, msdMET.address)
).revertedWithCustomError(pool, 'RemainingDebtIsLowerThanTheFloor');
await expect(
  pool.liquidate(msEth.address, attacker.address, fullDebt, msdMET.address)
).revertedWithCustomError(pool, 'AmountIsTooHigh');

// 5. Debt persists and grows; position is permanently unliquidatable.
```

Note: validity depends on the deployed `debtFloorInUsd` value being non-zero; the deployment artifacts expose the parameter but I could not confirm the on-chain configured value from the indexed files. If `debtFloorInUsd == 0` on all live pools, this analog reduces to a config-dependent edge case rather than a reachable bug.