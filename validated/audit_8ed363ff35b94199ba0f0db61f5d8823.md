### Title
Fully-liquidated underwater positions keep accruing interest on residual debt despite holding zero collateral - (contracts/Pool.sol)

### Summary
When a position's collateral value falls below its debt value (LTV > 100%), `Pool.liquidate` allows a liquidator to seize the user's entire `DepositToken` balance while only partially burning the user's `DebtToken` balance. The residual debt principal continues to accrue interest via the global `debtIndex` — growing the user's liability and minting unbacked interest-fee synths to `feeCollector` — even though the user no longer has any collateral in the pool.

### Finding Description
The bug class from the StakeWise report — "a fully liquidated position retains residual shares that keep accumulating fees" — maps directly onto Metronome's liquidation and interest-accrual design:

1. `Pool.liquidate` seizes collateral proportional to `amountToRepay_` via `quoteLiquidateOut`, and only reverts if `_totalSeized > depositToken_.balanceOf(account_)` (Pool.sol:581-585). When the account is underwater, `quoteLiquidateMax` caps repayment at what the entire deposit balance is worth (Pool.sol:429-439), so a liquidator can seize ~100% of collateral while leaving `debtToken.balanceOf(account_) > 0`. The protocol's own tests confirm this end state: `depositSeized ≈ depositBefore`, `msdMET.balanceOf(alice) ≈ 0`, and `msEthDebtToken.balanceOf(alice) > 0` (test/Pool.test.ts:811-815, 861-865).

2. `Pool.liquidate` only burns `amountToRepay_` of debt (Pool.sol:588); it does not check whether the account's total collateral went to zero, nor does it write off or freeze the residual debt.

3. The residual `principalOf[account]` keeps accruing interest: `DebtToken.balanceOf` multiplies the stored principal by `debtIndex / debtIndexOf[account]` (DebtToken.sol:196-206), and `accrueInterest` grows `debtIndex` and mints the accrued interest as fresh synthetic tokens to `pool.feeCollector()` (DebtToken.sol:169-178). The accrued interest on fully-unbacked residual debt therefore both inflates the user's liability and mints unbacked msAssets to the fee collector.

4. `debtFloorInUsd` (Pool.sol:571-579) only forces residual debt to be either 0 or ≥ floor — it does not prevent the underwater-full-seizure case where residual debt is above the floor.

If the user later re-deposits collateral, `debtPositionOf` counts the inflated residual debt against the new deposit, reducing `_issuableInUsd` and potentially leaving the position instantly unhealthy/liquidatable — mirroring the report's "re-stake after a long period" scenario.

### Impact Explanation
- Users whose collateral is fully seized retain a debt balance that silently compounds at `interestRate`, punishing them with a larger-than-expected liability if they ever re-enter the pool.
- `accrueInterest` mints interest-fee synths to `feeCollector` backed partly by unbacked residual debt — these fees are unrecoverable in practice (the debtor has no collateral to seize), so the treasury effectively realizes unbacked msAsset supply, diluting the synthetic asset and worsening protocol insolvency accounting.

### Likelihood Explanation
Requires a position to go underwater (collateral USD < debt USD), which happens during sharp collateral price drops or gap liquidations — a normal market condition, not an edge case. The triggering call (`liquidate` with `amountToRepay_ = quoteLiquidateMax(...)`) is permissionless. No privileged role, oracle manipulation, or governance action is needed. Severity is bounded by the residual-debt size and elapsed time, consistent with a Medium.

### Recommendation
In `Pool.liquidate` (or `DebtToken.burn` path), when the seized amount exhausts the account's collateral — e.g., when `_totalSeized >= depositToken_.balanceOf(account_)` prior to seizure across all of the account's deposit tokens — settle the position explicitly: write off the residual `principalOf`/`debtIndexOf` (socialize as bad debt) rather than letting it keep indexing interest. At minimum, stop interest accrual on positions whose total collateral is zero.

### Proof of Concept
Hardhat (repo's own test scaffolding demonstrates the end state; extend as follows):

```ts
// test/Pool.test.ts context: alice deposited MET, minted msETH; MET price halved
await masterOracle.updatePrice(met.address, toUSD('0.50'))

// collateral:debt < 1 — position is underwater
const amountToRepay = await pool.quoteLiquidateMax(msEth.address, alice.address, msdMET.address)
await pool.connect(liquidator).liquidate(msEth.address, alice.address, amountToRepay, msdMET.address)

// 1) essentially all collateral seized, but residual debt remains
expect(await msdMET.balanceOf(alice.address)).to.be.closeTo(0, 6000)
const debtBefore = await msEthDebtToken.balanceOf(alice.address)
expect(debtBefore).to.be.gt(0) // residual unbacked debt

// 2) residual debt keeps accruing interest despite zero collateral
await ethers.provider.send('evm_increaseTime', [365 * 24 * 3600])
await msEthDebtToken.accrueInterest()
const debtAfter = await msEthDebtToken.balanceOf(alice.address)
expect(debtAfter).to.be.gt(debtBefore) // grows at interestRate with no collateral backing

// 3) if alice re-deposits, the inflated residual debt counts against her
await msdMET.connect(alice).deposit(met.address, depositAmount)
const { _debtInUsd, _issuableInUsd } = await pool.debtPositionOf(alice.address)
// _debtInUsd includes compounded residual debt; _issuableInUsd reduced / unhealthy
```

Key code path: `Pool.liquidate` → `quoteLiquidateOut` → `depositToken_.seize` (full balance) while `_debtToken.burn` only reduces debt partially (Pool.sol:581-592); residual principal then compounds via `DebtToken.balanceOf`/`accrueInterest` (DebtToken.sol:156-206).