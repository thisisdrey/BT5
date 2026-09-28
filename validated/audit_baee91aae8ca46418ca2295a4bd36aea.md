### Title
Positions can be made permanently unliquidatable by the `debtFloorInUsd`/`AmountIsTooHigh` interaction, locking in bad debt — (`contracts/Pool.sol:571-585`)

### Summary
`Pool.liquidate` requires that a liquidation either fully repays the account's debt in that synthetic token or leaves a remaining debt at or above `debtFloorInUsd`. For a deeply underwater account (collateral value < debt value), a full repayment is impossible because `quoteLiquidateOut` produces a `_totalSeized` greater than the account's deposit balance, reverting with `AmountIsTooHigh`. Any partial repayment that fits within the seizable collateral necessarily leaves residual debt below the floor, reverting with `RemainingDebtIsLowerThanTheFloor`. The account can therefore never be liquidated, its debt (plus accrued interest) is never repaid, and the synthetic token supply becomes permanently undercollateralized — the same bug class as "an unliquidatable counterparty accumulates losses that the protocol and its users must absorb."

### Finding Description
In `Pool.liquidate` (contracts/Pool.sol:537-596):

```solidity
if (debtFloorInUsd > 0) {
    uint256 _newDebtInUsd = masterOracle().quoteTokenToUsd(
        address(syntheticToken_),
        _debtTokenBalance - amountToRepay_
    );
    if (_newDebtInUsd > 0 && _newDebtInUsd < debtFloorInUsd) {
        revert RemainingDebtIsLowerThanTheFloor();
    }
}

(_totalSeized, _toLiquidator, _fee) = quoteLiquidateOut(syntheticToken_, amountToRepay_, depositToken_);

if (_totalSeized > depositToken_.balanceOf(account_)) {
    revert AmountIsTooHigh();
}
```

- `quoteLiquidateOut` computes `_totalSeize` as the oracle value of `amountToRepay_` plus liquidator incentive and protocol fee, so for an underwater account the repayment required to seize the full collateral is strictly less than the debt.
- `quoteLiquidateMax` caps `amountToRepay_` at what the collateral covers, which always leaves positive residual debt — and once the account is sufficiently underwater, that residual is worth less than `debtFloorInUsd`.
- Repaying the *full* debt exceeds the seizable collateral → `AmountIsTooHigh`. Repaying anything less leaves debt in `(0, debtFloorInUsd)` → `RemainingDebtIsLowerThanTheFloor`. Every code path reverts.
- This is reachable by a fully unprivileged user: anyone can deposit collateral, `DebtToken.issue` the maximum allowed (`issue` enforces the floor only on the *resulting* debt, not on future liquidability), and a subsequent collateral price decline makes the position permanently unliquidatable. No privileged action is needed; `debtFloorInUsd > 0` is a deployed configuration the code explicitly supports (`updateDebtFloor`, exercised in test/Pool.test.ts:408-437).

### Impact Explanation
Bad debt is permanent. The DebtToken continues accruing interest on the unbacked principal, the corresponding `SyntheticToken` supply has no collateral backing it, and the protocol — and thereby all holders/redeemers of the synthetic asset — absorb the loss. This is protocol insolvency, matching the report's "unliquidatable counterparty creates loss of funds" class.

### Likelihood Explanation
Requires `debtFloorInUsd > 0` and an account whose collateral value falls below its debt value (or below `debtFloorInUsd`-compatible bounds). An attacker can deliberately manufacture this by opening a minimally-margined position sized just above the floor and letting normal volatility push it underwater — no oracle manipulation, admin role, or keeper cooperation needed. Even absent an attacker, any naturally underwater small position is frozen forever.

### Recommendation
When full repayment would exceed the seizable collateral, allow the liquidation to proceed by capping `amountToRepay_` at `quoteLiquidateMax` *and* waiving the debt-floor check for the residual (write it off / socialize the remainder), rather than reverting. Alternatively, exempt accounts whose total collateral value is below `debtFloorInUsd` from the `RemainingDebtIsLowerThanTheFloor` check so the position can always be fully seized and closed.

### Proof of Concept
Hardhat sketch, extending `test/Pool.test.ts` setup (alice deposits `msdMET`, issues `msETH`):

```ts
it('underwater position below debt floor cannot be liquidated', async function () {
  // given: governor-configured floor and a fully margined position
  await pool.updateDebtFloor(parseEther('3000'))                 // $3,000 floor
  // alice deposited ~$5,000 MET and issued 1 msETH ($4,000 debt) — just above floor

  // price crash: MET falls so collateral < debt (existing pattern in test file, L773-781)
  await masterOracle.updatePrice(met.address, toUSD('0.50'))

  // when: full repayment — seizes more than the account has
  const debt = await msEthDebtToken.balanceOf(alice.address)
  await expect(
    pool.connect(liquidator).liquidate(msEth.address, alice.address, debt, msdMET.address)
  ).revertedWithCustomError(pool, 'AmountIsTooHigh')             // matches test L783-789

  // and: max seizable partial repayment — leaves residual debt below the floor
  const maxRepay = await pool.quoteLiquidateMax(msEth.address, alice.address, msdMET.address)
  await expect(
    pool.connect(liquidator).liquidate(msEth.address, alice.address, maxRepay, msdMET.address)
  ).revertedWithCustomError(pool, 'RemainingDebtIsLowerThanTheFloor')

  // then: no liquidate path exists; debt + interest remain forever
  expect(await msEthDebtToken.balanceOf(alice.address)).eq(debt)
})
```

Caveat: the exact breakeven where *both* reverts cover the whole repayment space depends on `debtFloorInUsd` vs. collateral value; the PoC parameters (floor $3,000 vs. $4,000 debt with ~50% collateral crash) mirror the existing test fixture and should be tuned if run against different deployed parameters.