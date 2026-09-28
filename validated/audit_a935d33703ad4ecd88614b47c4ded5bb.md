### Title
`Pool.liquidate()` reverts instead of clamping when the repayable/seizable amount shrinks, letting a competing liquidator front-run and monopolize liquidation incentives — ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
Analogous to the Escher `LPDA.buy()` "TOO MANY" revert, `Pool.liquidate()` treats a user-supplied `amountToRepay_` as an exact order that must fit the *current* liquidable amount. If the position's remaining debt or remaining seizable collateral shrinks between transaction submission and execution, the call reverts with `AmountIsTooHigh` rather than clamping the repay amount to the remaining maximum. A competing liquidator can exploit this to repeatedly invalidate other liquidators' pending transactions by executing a small liquidation first, capturing the entire liquidation incentive for themselves.

### Finding Description
`quoteLiquidateMax()` computes the maximum repay amount as the minimum of (a) the synthetic amount needed to seize the account's full collateral balance and (b) `debtToken.balanceOf(account) * maxLiquidable` (initialized to 50%):

```solidity
// contracts/Pool.sol
_maxAmountToRepay = debtTokenOf[syntheticToken_].balanceOf(account_).wadMul(maxLiquidable);
if (_amountToRepay < _maxAmountToRepay) {
    _maxAmountToRepay = _amountToRepay;
}
```

The matching `liquidate()` path (and its `quoteLiquidateIn`/`quoteLiquidateOut` helpers) computes the collateral to seize from `amountToRepay_` and reverts when that amount exceeds what the position can still support — the test suite explicitly documents `revert AmountIsTooHigh` when "paying more than needed to seize all deposit" (test/Pool.test.ts:783-789). There is no clamp-to-max logic analogous to the recommended `newId = temp.finalId` mitigation.

Exploit trace (all unprivileged, public entry point `Pool.liquidate(syntheticToken_, account_, amountToRepay_, depositToken_)`):

1. Victim position becomes unhealthy (e.g., collateral:debt < 1 so the full deposit is seizable).
2. Alice (liquidator) computes `maxRepay = quoteLiquidateMax(msETH, victim, msdMET)` off-chain / in a bot and submits `liquidate(msETH, victim, maxRepay, msdMET)`.
3. Bob observes the mempool and front-runs with `liquidate(msETH, victim, dust, msdMET)` — repaying a small amount and seizing `dust * (1 + liquidatorIncentive)` of collateral.
4. Alice's `amountToRepay_` now exceeds the remaining seizable collateral value → her transaction reverts `AmountIsTooHigh`.
5. Bob repeats: whenever Alice resubmits with a recalculated amount, Bob front-runs another partial liquidation. Because the recompute-then-submit window always exists on-chain, Bob can keep Alice's transactions reverting while he drains the position's collateral at the incentive-discounted price.

This is the same structural flaw as `require(newId <= temp.finalId)`: an exact-fit check against mutable shared state that a front-runner can shrink to DoS competing buyers/liquidators.

### Impact Explanation
Direct economic harm: liquidation incentive (the `_liquidatorIncentive` fee split paid in seized collateral in `quoteLiquidateOut`/`DepositToken.seize`) is systematically captured by the front-runner while competing liquidators are forced to revert and burn gas. In liquidation-race conditions (sharp price moves), this can also delay liquidation execution, increasing the risk that positions go further underwater before being liquidated — a mild solvency concern. The attacker needs only to hold the synthetic token for repayment, which is freely mintable via `DebtToken.issue` against their own collateral, so capital cost is low.

### Likelihood Explanation
Medium. Liquidation front-running is inherent competitive MEV in all lending protocols, and the revert itself is standard EVM behavior — a victim can always re-quote and resubmit. However, the persistence of the attack is what elevates it beyond normal competition: Bob pays only gas for each dust liquidation and can repeat indefinitely within the same unhealthy-position window, so a sophisticated attacker can reliably monopolize large liquidations. The deployed config enables it whenever `liquidatorIncentive > 0` and a position's full deposit is seizable (collateral:debt < 1), which is exactly the deep-underwater scenario where liquidation competition and the incentive payout are largest.

Caveat: if judges treat liquidation racing as accepted protocol behavior, this may be downgraded — the novel element over normal MEV is specifically that the hard-revert (rather than clamping) makes victim transactions deterministically fail instead of executing partially.

### Recommendation
Mirror the LPDA fix: clamp instead of revert. In `liquidate()`, compute the current max repayable amount (the `quoteLiquidateMax` logic inline) and reduce `amountToRepay_` to it when it exceeds the bound, burning/seizing only for the clamped amount:

```solidity
uint256 _max = /* min(maxLiquidable * debt, amount covering remaining collateral) */;
if (amountToRepay_ > _max) {
    amountToRepay_ = _max;
}
// then proceed to quoteLiquidateOut + repay + seize with the clamped amount
```

This lets a front-run victim's transaction still succeed for the remaining liquidable amount, so repeated dust front-runs can no longer indefinitely block competitors — the attacker only races for the incentive rather than monopolizing it via forced reverts.

### Proof of Concept
Hardhat-style sketch against the existing fixture pattern in test/Pool.test.ts (deeply-underwater branch where the full deposit is seizable):

```typescript
// setup: alice's vaDAI->msUSD position is underwater (collateral:debt < 1)
// such that quoteLiquidateMax covers nearly all of her msdVaDAI deposit

const maxRepay = await pool.quoteLiquidateMax(msUSD.address, alice.address, msdVaDAI.address)

// bob (griefer/attacker) holds his own msUSD and front-runs with a dust liquidation
const dust = maxRepay.div(1000)
await pool.connect(bob).liquidate(msUSD.address, alice.address, dust, msdVaDAI.address)

// victim liquidator's precomputed max-liquidate tx now reverts
await expect(
  pool.connect(liquidator).liquidate(msUSD.address, alice.address, maxRepay, msdVaDAI.address)
).to.be.revertedWithCustomError(pool, 'AmountIsTooHigh')

// repeat: bob keeps submitting dust liquidations ahead of each resubmission,
// seizing collateral at the incentive rate while all competing txns revert
```

The existing test at test/Pool.test.ts:783-789 already proves the revert primitive ("should revert if paying more than needed to seize all deposit"); the PoC composes it with front-running to show repeatability.