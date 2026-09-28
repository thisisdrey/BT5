### Title
Underwater positions are never fully liquidatable: residual bad debt is left permanently unbacked once collateral is exhausted - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool.liquidate` only supports pro-rata liquidations: the collateral seized is always `repay * (1 + liquidatorIncentive + protocolFee)`, and the call reverts with `AmountIsTooHigh` when that exceeds the account's deposit balance (Pool.sol:581-585). There is no "clean liquidation"/bad-debt write-off path (contrast with Fraxlend's `liquidateClean`). Once a position's collateral value falls below the seized-amount needed to cover the debt plus incentives, a liquidator can at most seize the entire remaining collateral for a partial repayment, leaving residual `DebtToken` debt on the account with ~zero collateral. That residual debt is permanently unbacked: no actor has any incentive to repay it, and the corresponding synthetic tokens remain in circulation. `debtFloorInUsd` does not prevent this — it only constrains the *remaining debt after a liquidation call*, not after market moves, and it actually guarantees a residual debt of at least `debtFloorInUsd` with no collateral backing it.

### Finding Description
In `liquidate` (Pool.sol:537-596):

```solidity
(_totalSeized, _toLiquidator, _fee) = quoteLiquidateOut(syntheticToken_, amountToRepay_, depositToken_);
if (_totalSeized > depositToken_.balanceOf(account_)) {
    revert AmountIsTooHigh();
}
```

- `quoteLiquidateOut` (Pool.sol:451-472) prices `_totalToSeize = repay_value * (1 + incentive + fee)` via `masterOracle`. The liquidator's profit is strictly proportional to the repaid amount.
- `quoteLiquidateMax` (Pool.sol:419-440) caps repayment at the amount whose seize equals the *entire* collateral balance. Executing that max repay leaves `depositToken_.balanceOf(account_) ≈ 0` while `_debtToken.balanceOf(account_) > 0`.
- The `debtFloorInUsd` check (Pool.sol:571-579) forces the liquidator to leave at least `debtFloorInUsd` of residual debt (or repay in full — impossible when collateral is exhausted). So the terminal state of an underwater position is: collateral ≈ 0, debt ≥ `debtFloorInUsd`, forever.
- Any subsequent `liquidate` on this account can seize only dust collateral, yielding profit far below gas cost. There is no fallback mechanism (no auction, no socialization, no treasury backstop callable by an unprivileged user) to clear it.

Invariant broken: solvency — circulating `SyntheticToken` supply is backed by less collateral + recoverable debt than its face value; the deficit is absorbed implicitly by synth holders and the pool.

### Impact Explanation
Permanent protocol insolvency proportional to the sum of residual debts on underwater accounts. Each deeply-underwater position leaves unbacked synthetic supply that can never be reclaimed through liquidation, diluting all synth holders and, in Metronome's design, implicitly the swap counterparty pool. Unlike the Fraxlend original there is not even a `liquidateClean` that a charitable/altruistic actor could call — the bad debt is structurally uncleanable by any unprivileged account.

### Likelihood Explanation
Requires positions to become deeply underwater (collateral value < debt × (1 + incentives)). This occurs naturally during sharp collateral-price drawdowns, oracle update gaps, or for low-collateral-factor/volatile collateral types; no privileged action or oracle manipulation is needed. High gas environments make the dust-liquidation disincentive bind earlier, exactly as in the original finding. Partial liquidations that take ~all collateral are already the profit-maximizing move for liquidators (`quoteLiquidateMax`), so the terminal dust-collateral state is the expected outcome, not an edge case.

### Recommendation
Add a clean-liquidation/write-off path: when `quoteLiquidateOut` for the full remaining debt exceeds the collateral balance, allow a liquidator (or anyone) to repay up to the full debt while seizing the entire remaining collateral, even if the seize is less than `repay * (1 + fees)`. Complement `debtFloorInUsd` with a collateral-dust floor: either forbid liquidations that would leave collateral below a USD threshold unless the whole debt is repaid, or permit seize-all liquidations below that threshold. Optionally socialize residual debt (e.g., cover from protocol fees / fee collector) when collateral hits zero.

### Proof of Concept
Hardhat fork sketch (mirrors `test/Pool.test.ts` liquidation setup):

```ts
// setup: alice deposits MET via msdMET, mints msEth; liquidator holds msEth
// 1. crash collateral price so position is deeply underwater
await masterOracle.updatePrice(met.address, toUSD('0.01')) // -99.5%

// 2. liquidator repays the max allowed (seizes ~all collateral)
const maxRepay = await pool.quoteLiquidateMax(msEth.address, alice.address, msdMET.address)
await pool.connect(liquidator).liquidate(msEth.address, alice.address, maxRepay, msdMET.address)

// 3. terminal state: dust collateral, residual debt >= debtFloorInUsd
expect(await msdMET.balanceOf(alice.address)).to.be.lte(dust)            // ~0
const residualDebt = await msEthDebtToken.balanceOf(alice.address)
expect(residualDebt).to.be.gt(0)                                       // bad debt remains

// 4. no further profitable liquidation exists:
const next = await pool.quoteLiquidateMax(msEth.address, alice.address, msdMET.address)
// next repay seizes only dust -> incentive < gas; and any attempt to seize
// more than balance reverts:
await expect(
  pool.connect(liquidator).liquidate(msEth.address, alice.address, residualDebt, msdMET.address)
).to.be.revertedWithCustomError(pool, 'AmountIsTooHigh')

// 5. residualDebt is never burned: msEth supply stays unbacked -> insolvency
```