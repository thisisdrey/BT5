### Title
Liquidations can execute while the protocol is paused (and immediately after unpause) while collateral deposits are disabled, leaving borrowers no grace period to top up margin - ([File: contracts/Pool.sol])

### Summary
`Pool.pause()` only disables collateral deposits. `Pool.liquidate()` is guarded by `whenNotShutdown`, not `whenNotPaused`, so it remains callable by any EOA while the pool is paused and in the very same block as `unpause()`. During a pause, `DebtToken.accrueInterest()` keeps inflating borrowers' debt, which can push positions below the health threshold, yet borrowers cannot add collateral because `DepositToken.deposit()` requires `Pool.paused() == false`. A permissionless liquidator can therefore seize collateral the moment a position turns unhealthy — including the first block after unpause — with no buffer for users to react.

### Finding Description
- `Pauseable.pause()`/`unpause()` have no time buffer; `unpause()` simply flips `_paused` in the same transaction it is called (contracts/utils/Pauseable.sol:105-125).
- `Pool.liquidate()` is protected only by `whenNotShutdown` (contracts/Pool.sol:537-545), and it calls `debtToken.accrueInterest()` first, so all interest accumulated during the pause is charged before the health check `debtPositionOf(account_)` (contracts/Pool.sol:556-563). A position healthy when the pause began can become liquidatable purely from accrued interest.
- Per docs/emergency-flags.md, `DepositToken.deposit()` is disabled when `Pool.paused()` or `PoolRegistry.paused()`, while `Pool.liquidate()` is only disabled on shutdown. The suite even asserts this asymmetry: `test/Pool.test.ts` "should not revert if paused" liquidates a user while `pool.pause()` is active (test/Pool.test.ts:353-365).
- Unlike the FlatMoney report, there is no execution-delay ordering issue here — it is worse: liquidation never stops, only the margin-increase path (deposit) does.

Caveat: borrowers can still call `DebtToken.repay()`/`repayAll()` and `Pool.swap()` during a pause (all `whenNotShutdown` only), so a user holding or able to acquire the msAsset can restore health without depositing. The grief hits borrowers whose only defense is adding collateral (e.g., they hold the underlying, not the synth).

### Impact Explanation
Direct loss of user funds: accrued interest during a pause can make a position unhealthy, and any liquidator can seize collateral (plus liquidation fee) while the borrower's deposit path is blocked or before they can react post-unpause. `maxLiquidable` (up to 100% of debt) and `DepositToken.seize` determine the size of the loss.

### Likelihood Explanation
Requires a governor/guardian pause event plus interest accrual sufficient to push a near-threshold position under water. Pauses are rare, but interest accrual is deterministic and permissionless, and liquidation is open to any EOA holding the synth, so whenever the precondition holds, execution is guaranteed and costless to the liquidator. No front-running is needed — `liquidate` succeeds even in the same block as `unpause`.

### Recommendation
Either gate `Pool.liquidate()` (and `DepositToken.seize` invoked through it) with `whenNotPaused`, or record an `unpausedAt` timestamp in `Pauseable.unpause()` and require `block.timestamp >= unpausedAt + GRACE_PERIOD` inside `liquidate`, so borrowers get a window to deposit collateral or repay before liquidations resume.

### Proof of Concept
Hardhat sketch, mirroring the existing test harness in `test/Pool.test.ts`:

```ts
it('liquidates a position turned unhealthy by interest accrued during pause', async function () {
  // alice deposits msdMET and issues msEth near the liquidation threshold
  // governor pauses deposits only
  await pool.pause()
  expect(await pool.paused()).true

  // alice cannot add margin
  await expect(msdMET.connect(alice).deposit(parseEther('1'))).reverted

  // time passes; debt grows via accrued interest
  await ethers.provider.send('evm_increaseTime', [30 * 24 * 3600])
  await msEthDebtToken.accrueInterest()
  const { _isHealthy } = await pool.debtPositionOf(alice.address)
  expect(_isHealthy).false

  // any EOA liquidates while still paused — succeeds
  await pool.connect(liquidator).liquidate(msEth.address, alice.address, amountToRepay, msdMET.address)

  // alternatively: unpause and liquidate in the same block, no grace period
  // await pool.unpause(); await pool.connect(liquidator).liquidate(...)
})
```

This reproduces on the existing fixture: the repo's own test already proves `liquidate` succeeds while paused; the only addition is letting interest accrue so the health flip is caused solely by the pause duration.