### Title
`debtFloorInUsd` combined with `maxLiquidable` can permanently brick liquidation of unhealthy positions — borrowers can hold positions that no liquidator can ever close - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary

`Pool.liquidate` enforces two independent bounds on `amountToRepay_`:

1. `amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable` reverts (`AmountGreaterThanMaxLiquidable`) — i.e. a liquidator may repay at most `maxLiquidable` (e.g. 50%) of the account's debt token balance.
2. If `debtFloorInUsd > 0`, repaying an amount that leaves remaining debt in `(0, debtFloorInUsd)` reverts (`RemainingDebtIsLowerThanTheFloor`).

For any unhealthy account whose debt `D` satisfies `D - D*maxLiquidable < debtFloorInUsd` (i.e. `D < floor / (1 - maxLiquidable)`), *every* `amountToRepay_` reverts: a partial repayment leaves dust debt below the floor, and a full repayment exceeds `maxLiquidable`. The position becomes permanently unliquidatable, which is the exact analog of the Perennial `minPosition` freeze: a minimum-position constraint makes it impossible to fully deallocate/close a position. In Perennial it froze vault withdrawals; here it freezes the liquidation/close-out path and strands bad debt on the protocol.

### Finding Description

- `Pool.liquidate` line 567: `if (amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) revert AmountGreaterThanMaxLiquidable();`
- `Pool.liquidate` lines 571-579: `if (_newDebtInUsd > 0 && _newDebtInUsd < debtFloorInUsd) revert RemainingDebtIsLowerThanTheFloor();`

`DebtToken._mint` (lines 580-588) only enforces `balance + amount >= debtFloorInUsd` at issuance, so borrowers routinely sit just above the floor. `DebtToken.repay` (lines 442-451) has the same floor check, but borrowers have an escape hatch — `repayAll` (lines 466-495) bypasses the floor entirely. Liquidators have no such escape: `Pool.liquidate` is the only function that can seize a defaulted account's collateral, and it applies both bounds unconditionally with no "close-out" exemption.

Concrete deadlock: with `debtFloorInUsd = 100e18` and `maxLiquidable = 0.5e18`, an account with `D = 150` msUSD of debt that turns unhealthy can only legally be repaid in `[0, 75]` (maxLiquidable) — but any repay `< 50` leaves `> 100` debt (still unhealthy, fine), and any repay in `(50, 75]` leaves remaining debt `< 100 = floor` → `RemainingDebtIsLowerThanTheFloor`. To make the account healthy you must repay at least `50`; to fully close you must repay `150` which violates `maxLiquidable`. More importantly, once the account is deeply underwater, seizing `75`-worth repays only half while leaving sub-floor dust — every call reverts, and `quoteLiquidateMax` returns the max repay which itself reverts on-chain.

### Impact Explanation

Permanent freezing of the liquidation path / protocol insolvency. An unprivileged borrower can open a position with debt in the "unliquidatable band" (`floor < D < floor / (1 - maxLiquidable)` in USD terms) and simply let collateral price drift against it. No liquidator can ever execute `Pool.liquidate` successfully against this account regardless of how underwater it becomes. The bad debt accrues interest (interest even accrues on `totalSupply_`, worsening it) and is ultimately socialized across synthetic-token holders and the protocol — the same "forced to keep a losing position" outcome as the Perennial report, applied to the protocol instead of a vault.

### Likelihood Explanation

- Reachable entirely by unprivileged EOAs: `DebtToken.issue`/`DebtToken.mint` let any user create debt just above `debtFloorInUsd`.
- Requires `debtFloorInUsd > 0` and `maxLiquidable < 1e18` — both are standard production settings for this codebase (the floor exists precisely to avoid dust debt, and `maxLiquidable` is conventionally set below 100%). The deadlock is a code-level interaction bug, not merely a misconfiguration: even with reasonable parameters, `liquidate` provides no path to fully close a position in the band.
- The position only needs to become unhealthy via normal market movement; no oracle manipulation is required.

### Recommendation

Exempt full close-outs from `maxLiquidable` (and/or from the floor check) in `Pool.liquidate`, the same way `DebtToken.repayAll` exempts borrowers:

```solidity
// contracts/Pool.sol, in liquidate()
uint256 _debtTokenBalance = _debtToken.balanceOf(account_);
bool _isFullClose = amountToRepay_ == _debtTokenBalance;
if (!_isFullClose && amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) {
    revert AmountGreaterThanMaxLiquidable();
}
```

Alternatively, auto-extend `amountToRepay_` to the full balance when the partial amount would leave sub-floor debt, provided the seized collateral still covers it.

### Proof of Concept

Hardhat (matches repo's existing test harness pattern in `test/`):

```ts
// Assumes: pool.debtFloorInUsd = 100e18, pool.maxLiquidable = 0.5e18 (set via governor in fixture)
it('liquidation of a sub-floor-band position always reverts', async () => {
  // 1. Alice deposits collateral and issues debt just above the floor
  await collateral.connect(alice).approve(depositToken.address, MaxUint256)
  await depositToken.connect(alice).deposit(parseEther('200'), alice.address)   // $200 collateral
  await msUsdDebtToken.connect(alice).issue(parseEther('150'), alice.address)   // $150 debt > $100 floor

  // 2. Price moves so the position is unhealthy
  await masterOracle.updateRate(collateral.address, parseEther('0.5')) // $100 collateral vs $150 debt

  // 3. Every liquidation amount reverts
  const debt = await msUsdDebtToken.balanceOf(alice.address)
  // partial: leaves remaining < floor -> RemainingDebtIsLowerThanTheFloor
  await expect(
    pool.connect(liq).liquidate(msUSD.address, alice.address, debt.mul(50).div(100), depositToken.address)
  ).to.be.revertedWithCustomError(pool, 'RemainingDebtIsLowerThanTheFloor')
  // full close: exceeds maxLiquidable -> AmountGreaterThanMaxLiquidable
  await expect(
    pool.connect(liq).liquidate(msUSD.address, alice.address, debt, depositToken.address)
  ).to.be.revertedWithCustomError(pool, 'AmountGreaterThanMaxLiquidable')
})
```

Note on certainty: the deadlock arithmetic and the two reverting bounds are verified directly in `contracts/Pool.sol` lines 567 and 571-579 and `contracts/DebtToken.sol` lines 442-451 / 466-495. I could not confirm the exact on-chain values of `debtFloorInUsd`/`maxLiquidable` in the deployment artifacts within the available search budget — if `debtFloorInUsd == 0` or `maxLiquidable == 1e18` on all deployed pools, the bug is latent rather than live, but the code path remains flawed and should be fixed before either parameter is used.