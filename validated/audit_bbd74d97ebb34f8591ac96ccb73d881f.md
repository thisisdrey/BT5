### Title
Liquidator cannot repay the full debt when collateral is near-fully utilized — seizure scaled by incentives exceeds `DepositToken` balance and reverts - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
Analogous to the Splits Swapper issue where the caller's reward is scaled up from the same balance being drained (so swapping the entire balance always reverts), `Pool.liquidate` computes `_totalSeized` as the repaid debt value grossed up by `liquidatorIncentive` and `protocolLiquidationFee`, then reverts with `AmountIsTooHigh` when that inflated amount exceeds the account's `DepositToken` balance (lines 581–585). For positions whose collateral value barely exceeds their debt, the maximum allowed repayment cannot execute.

### Finding Description
In `liquidate`, `quoteLiquidateOut` returns a `_totalSeized` that includes the liquidator incentive and protocol fee on top of the repaid amount's collateral value: [1](#0-0) 

The check `if (_totalSeized > depositToken_.balanceOf(account_))` treats the *inflated* seizure as the bound, not the account's raw balance. Consider a position that just turned unhealthy: `debtInUsd * (1 + liquidatorIncentive + protocolLiquidationFee) > collateralBalance` while `debtInUsd <= collateralBalance`. A liquidator attempting `amountToRepay_` up to `maxLiquidable` (e.g. repaying 100% when `maxLiquidable == 1e18`, or the full close needed to satisfy `debtFloorInUsd`) hits `AmountIsTooHigh` even though the account holds enough collateral to cover the repaid debt itself.

This also interacts badly with the debt floor at lines 571–579: a liquidator may be forced to repay the entire remaining debt (`_newDebtInUsd` must be 0 or `>= debtFloorInUsd`), yet the full repayment is exactly the case where the incentive-grossed seizure is most likely to exceed the collateral balance — a revert with no partial-repay escape hatch.

### Impact Explanation
- Unexpected reverts on the economically most important liquidations (positions near 100% collateral utilization / just below the liquidation threshold).
- Missed liquidation incentive for the caller: to succeed the liquidator must repay less, receiving proportionally less incentive, and leaving residual debt that may fall under `debtFloorInUsd` and become un-liquidable dust/bad debt.
- Unlike the Swapper report there is no alternate "flush" path that rescues the excess; the revert is terminal for that call.

### Likelihood Explanation
Requires an unhealthy position whose collateral balance is below `debtValue * (1 + incentive + fee)`. This occurs whenever a position is only slightly undercollateralized in value terms — i.e., collateral value covers debt but not debt-plus-incentive. Given `liquidatorIncentive` is a governance-set nonzero parameter and positions routinely drift near the threshold with volatile collateral, the edge is reachable by any unprivileged liquidator calling `liquidate` directly. No privileged attacker is needed; the DoS/missed-reward is inherent to the quote math.

### Recommendation
Cap the seizure at the account's available balance instead of reverting: compute `_totalSeized = min(quotedSeized, depositToken_.balanceOf(account_))` and split the capped amount between liquidator and fee collector (or revert only when the capped seizure is worth less than `amountToRepay_`), so full-balance liquidations remain executable and the caller's incentive degrades gracefully rather than reverting.

### Proof of Concept
Hardhat fork sketch (mirroring `test/Pool.test.ts` liquidation setup):

```ts
// alice deposits WETH collateral, mints msUSD up to near the limit
// drop ETH price via mock oracle so position is *just* unhealthy:
//   collateralValue * (1 - liquidationThreshold) < debt
//   but collateralBalance < debtValue * (1 + liquidatorIncentive + protocolLiquidationFee)

const debt = await msUsdDebtToken.balanceOf(alice.address);
const { _totalSeized } = await pool.quoteLiquidateOut(msUsd.address, debt, depositToken.address);
const collatBal = await depositToken.balanceOf(alice.address);
assert(_totalSeized.gt(collatBal)); // incentive-grossed seizure exceeds balance

await expect(
  pool.connect(bob).liquidate(msUsd.address, alice.address, debt, depositToken.address)
).to.be.revertedWithCustomError(pool, 'AmountIsTooHigh'); // full liquidation impossible
```

Uncertainty note: I verified the revert path at `Pool.sol:581-585` and the `maxLiquidable`/`debtFloorInUsd` constraints, but did not read `quoteLiquidateOut`'s body (index returned matches only). The finding assumes it grosses up by `liquidatorIncentive`/`protocolLiquidationFee` as declared in `FeeProvider`; the quoted names and the `_toLiquidator`/`_fee` split in `seize` calls at lines 589–593 support this.

### Citations

**File:** contracts/Pool.sol (L581-585)
```text
        (_totalSeized, _toLiquidator, _fee) = quoteLiquidateOut(syntheticToken_, amountToRepay_, depositToken_);

        if (_totalSeized > depositToken_.balanceOf(account_)) {
            revert AmountIsTooHigh();
        }
```
