### Title
Race between `DebtToken.repay`/`Pool.liquidate` when `everythingStopped` is lifted — no grace window lets underwater borrowers repay before liquidators strike - (File: contracts/Pool.sol)

### Summary
`Pool.liquidate` and `DebtToken.repay`/`repayAll` are gated by the *same* `everythingStopped` flag (`whenNotShutdown`). While the flag is set, neither repaying nor liquidating is possible, but collateral prices and interest keep moving, so positions can become liquidatable during the shutdown. When the governor calls `open()`, both paths re-enable atomically — a liquidator bot can frontrun (or simply beat) the position owner's `repay` transaction and seize the collateral at a discount. There is no mechanism to re-enable repayments without simultaneously enabling liquidations. This is the same bug class as the referenced Blueberry finding, present verbatim in Metronome's pause design.

### Finding Description
- `Pool.liquidate` is guarded only by `whenNotShutdown` (Pool.sol:541-549): it reverts via `IsShutdown` only when `everythingStopped()` is true, and otherwise liquidates any unhealthy position immediately.
- `DebtToken.repay` and `DebtToken.repayAll` use the identical guard (`whenNotShutdown` → `pool.everythingStopped()`), so repayments are blocked for exactly the same period as liquidations (DebtToken.sol:418-424, 466-471).
- `Pauseable.open()` clears `_everythingStopped` in a single transaction with no timelock, no "repay-only" phase, and no flag distinguishing "repayments allowed" from "liquidations allowed" (Pauseable.sol:97-99). `pause()`/`unpause()` do not help either — deposits are the only thing `paused` gates; liquidations still run while paused (test/Pool.test.ts:353-365 confirms liquidation succeeds when paused).
- The flag also exists independently on `PoolRegistry` and `Pool` (docs/emergency-flags.md), but both layers still gate repay and liquidate together, so the race exists regardless of which layer is opened.

### Impact Explanation
An underwater position owner whose collateral was frozen during a shutdown loses collateral to a liquidation bonus seizure in the very first block after `open()`, despite being willing and able to repay. The liquidator (`Pool.liquidate`, Pool.sol:581-592) seizes `depositToken` collateral at a discount plus protocol fee — a direct, permanent loss of user funds that the owner could have avoided with even a single-block grace window. Since liquidators are automated bots monitoring mempool/state and owners are humans signing transactions, the race is structurally biased against the borrower.

### Likelihood Explanation
Likelihood is conditional on a shutdown event occurring while positions are near the liquidation threshold — shutdowns are rare admin actions, which keeps this at medium rather than high. However, when it does occur: (a) interest accrual and oracle price movement during the downtime push marginal positions underwater, (b) the winning liquidator only needs a public `liquidate` call — no privilege, no oracle manipulation, no reentrancy — and can frontrun the `open()` transaction's follow-on activity or backrun it in the same block via a bundle. Nothing in the code (health check in `liquidate`, `maxLiquidable` cap, `nonReentrant`) prevents it, since the liquidation is economically valid by that point.

### Recommendation
Allow repayments to be re-enabled before liquidations. Options: (1) add a separate `repayGracePeriodEnd`/`liquidationsEnabled` flag so `open()` first restores `repay`/`repayAll`/`withdraw` and only later flips `liquidate` on; (2) record `block.timestamp` in `open()` and have `Pool.liquidate` revert if `block.timestamp < openedAt + gracePeriod`; (3) never gate `repay`/`repayAll` at all — repaying debt is always safe for the protocol (the referenced Blueberry recommendation applies equally here).

### Proof of Concept
Hardhat fork-style sketch (real fork test required for the submission, following the existing setup in `test/Pool.test.ts`):

```ts
// Setup: alice has deposited msdMET and issued msEth; liquidator holds msEth.
// 1. Governor (or guardian) shuts the pool down:
await pool.connect(governor).shutdown(); // or guardian

// 2. During shutdown, oracle price moves so alice becomes unhealthy:
await masterOracle.updatePrice(met.address, toUSD('0.95'));
const { _isHealthy } = await pool.debtPositionOf(alice.address);
expect(_isHealthy).to.eq(false);

// 3. Alice tries to repay during shutdown -> blocked:
await expect(
  msEthDebtToken.connect(alice).repay(alice.address, amount)
).to.revertedWithCustomError(msEthDebtToken, 'IsShutdown');

// 4. Governor re-opens. Both repay and liquidate are live in the same block:
await pool.connect(governor).open();

// 5. Liquidator backruns open() (or frontruns alice's pending repay):
await pool
  .connect(liquidator)
  .liquidate(msEth.address, alice.address, amountToRepay, msdMET.address);

// Assert: alice lost collateral despite having msEth + intent to repay.
// Her repay now reverts or is moot; seized deposit tokens went to liquidator + feeCollector.
```

The same flow is reproducible via `PoolRegistry.shutdown()`/`open()` at the registry layer, since `Manageable.whenNotShutdown` on `DebtToken` and `Pool.liquidate` both resolve to the same atomic re-enable. No privileged or malicious actor is required for the harmful step — only a public `liquidate` call executed faster than the borrower's `repay`.