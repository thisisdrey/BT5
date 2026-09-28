### Title
Liquidation can seize all collateral before a pending `repayAll`, leaving the borrower to repay uncollateralized residual debt - ([File: contracts/DebtToken.sol](contracts/DebtToken.sol))

### Summary
`DebtToken.repay()` and `DebtToken.repayAll()` permit repayment of an account whose collateral has already been fully seized through `Pool.liquidate()`. A liquidator can front-run a borrower’s repayment transaction, seize the remaining collateral for a deeply unhealthy position, and the borrower’s pending repayment still executes against the residual bad debt.

### Finding Description
`Pool.liquidate()` is permissionless for unhealthy positions. It burns the liquidator’s synthetic tokens, burns part of the borrower’s debt, and transfers the quoted deposit-token amount to the liquidator and fee collector.

For a deeply undercollateralized position, `quoteLiquidateMax()` caps repayment by the amount needed to seize the account’s full deposit-token balance rather than by the outstanding debt. `DepositToken.seize()` transfers the collateral receipt tokens without requiring an unlocked balance. If the balance reaches zero, `_transfer()` removes the deposit token from the borrower’s collateral list.

Neither `DebtToken.repay()` nor `DebtToken.repayAll()` checks whether the debt position still has collateral. After liquidation removes the final collateral token, a pending `repayAll(onBehalfOf_)` still calculates the remaining debt, seizes the repay fee, burns the payer’s synthetic tokens, and burns the borrower’s residual debt.

Relevant flow:

1. The borrower holds synthetic tokens and submits `repayAll(borrower)` or `repay(borrower, amount)`.
2. The borrower’s position is unhealthy before the transaction executes.
3. An unprivileged liquidator front-runs it with `Pool.liquidate(syntheticToken, borrower, quoteLiquidateMax(...), depositToken)`.
4. The liquidation seizes substantially all, or all, of the borrower’s remaining deposit-token balance while leaving debt behind.
5. The borrower’s repayment then succeeds and burns additional synthetic tokens even though the collateral backing the debt has already been taken.

### Impact Explanation
The borrower loses the collateral through liquidation and additionally loses synthetic tokens through the queued repayment. The repayment provides no collateral-recovery benefit because the deposit token has already been removed from the account’s collateral set.

This is a direct loss of user funds caused by the absence of a post-liquidation repayment guard. The liquidator receives the normal collateral incentive, while the protocol’s remaining bad debt is reduced at the borrower’s expense.

### Likelihood Explanation
The condition requires an unhealthy or deeply undercollateralized position and a repayment transaction that executes after liquidation. Both `Pool.liquidate()` and the debt-token repayment functions are public and do not require privileged access.

The scenario is most likely when collateral prices deteriorate quickly or when a borrower broadcasts repayment near the liquidation threshold. A public mempool transaction can be ordered after a liquidation by any liquidator monitoring unhealthy positions.

### Recommendation
Prevent repayment of residual debt after all collateral backing a position has been liquidated.

For example, in `DebtToken.repay()` and `DebtToken.repayAll()`:

- query `pool.debtPositionOf(onBehalfOf_)` or `pool.depositOf(onBehalfOf_)`;
- revert when the account still has debt but has zero remaining collateral value; or
- track a dedicated fully-liquidated/bad-debt state and reject ordinary repayments while that state is active.

The check should distinguish a fully collateral-drained account from a merely unhealthy account, because repaying a still-collateralized unhealthy position is a valid recovery mechanism.

### Proof of Concept
The following Hardhat test can be added to the existing `test/Pool.test.ts` fixture. It assumes the fixture’s usual borrower deposit, synthetic-token issuance, and liquidator setup.

```ts
it('repays residual debt after liquidation seized all collateral', async function () {
  // Existing fixture state:
  // - Alice deposited 6,000 MET.
  // - Alice issued 1 msETH.
  // - MET collateral factor is 67%.
  // - maxLiquidable is 100%.
  // - Liquidator owns enough msETH to perform the liquidation.

  // Make Alice deeply undercollateralized:
  // 6,000 MET * $0.50 = $3,000 collateral, while 1 msETH = $4,000 debt.
  await masterOracle.updatePrice(met.address, toUSD('0.50'))

  const amountToRepay = await pool.quoteLiquidateMax(
    msEth.address,
    alice.address,
    msdMET.address
  )

  expect(amountToRepay).gt(0)
  expect(amountToRepay).lt(await msEthDebtToken.balanceOf(alice.address))

  // This represents Alice's pending repayment transaction.
  // A liquidator front-runs it and seizes Alice's remaining collateral.
  await pool
    .connect(liquidator)
    .liquidate(msEth.address, alice.address, amountToRepay, msdMET.address)

  const collateralAfterLiquidation = await msdMET.balanceOf(alice.address)
  const debtAfterLiquidation = await msEthDebtToken.balanceOf(alice.address)

  // All meaningful collateral is gone, but bad debt remains.
  expect(collateralAfterLiquidation).lt(parseEther('0.01'))
  expect(debtAfterLiquidation).gt(0)

  const synthBeforeRepay = await msEth.balanceOf(alice.address)

  // Alice's already-submitted repayAll still executes.
  await msEthDebtToken.connect(alice).repayAll(alice.address)

  const synthAfterRepay = await msEth.balanceOf(alice.address)

  expect(await msEthDebtToken.balanceOf(alice.address)).eq(0)
  expect(synthBeforeRepay.sub(synthAfterRepay)).eq(debtAfterLiquidation)
  expect(await msdMET.balanceOf(alice.address)).eq(collateralAfterLiquidation)
})
```

The key assertions are that `quoteLiquidateMax()` repays less than the full debt because collateral is exhausted first, the liquidation leaves positive residual debt, and `repayAll()` nevertheless burns Alice’s remaining synthetic tokens to clear that uncollateralized debt.