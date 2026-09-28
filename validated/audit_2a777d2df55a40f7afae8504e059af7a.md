### Title
Borrower can create unliquidatable positions via `maxLiquidable`/`debtFloorInUsd` interaction - (contracts/Pool.sol)

### Summary
`Pool.liquidate` enforces two mutually exclusive bounds on `amountToRepay_`: it cannot exceed `maxLiquidable` (initialized to 50%) of the account's debt token balance, and it cannot leave a positive remaining debt below `debtFloorInUsd`. Any user can deliberately size a debt position into the "dead zone" where no `amountToRepay_` satisfies both checks, permanently denying liquidation of that position — the same denial-of-liquidation class as the Symmetrical nonce-invalidation report, achieved here through parameter interaction rather than a signature nonce.

### Finding Description
`Pool.liquidate` at `contracts/Pool.sol:537` performs these checks in order:

1. `amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable` reverts with `AmountGreaterThanMaxLiquidable` (`contracts/Pool.sol:567-569`).
2. If `debtFloorInUsd > 0` and the post-repayment debt `_newDebtInUsd` is `> 0` and `< debtFloorInUsd`, it reverts with `RemainingDebtIsLowerThanTheFloor` (`contracts/Pool.sol:571-579`).

With `maxLiquidable = 0.5e18` (set in `initialize`, `contracts/Pool.sol:181`), consider an account whose debt is `D` USD with `debtFloorInUsd < D < debtFloorInUsd / (1 - maxLiquidable)` (i.e. `D < 2 * debtFloorInUsd`):

- Repaying the maximum allowed (`maxLiquidable * D`) leaves `0.5 * D < debtFloorInUsd` remaining → reverts.
- Repaying enough to land the remainder at/above the floor requires repaying more than `D - debtFloorInUsd`, i.e. a fraction `> maxLiquidable` → reverts.
- Repaying in full clears the floor check but is a fraction `1.0 > maxLiquidable` → reverts.

Every `amountToRepay_` reverts. The borrower constructs this entirely with public calls: `DepositToken.deposit` then `DebtToken.issue` (`contracts/DebtToken.sol:235`) to size the debt into the dead zone. Once the position turns unhealthy (price drift or interest accrual via `accrueInterest`), no liquidator can ever reduce it — it is permanently unliquidatable, symmetric to the original "PartyA can deny liquidations" bug. Note that interest accrual also pushes borderline positions *into* the dead zone over time, so even non-malicious small debts can become unliquidatable.

### Impact Explanation
Unhealthy positions sized in the dead zone can never be liquidated. If such a position becomes undercollateralized, the bad debt is permanent and is socialized across the pool/synthetic holders — protocol insolvency, not merely a temporary freeze. The attacker needs no privileged role; `deposit`/`issue` are permissionless.

### Likelihood Explanation
Requires `debtFloorInUsd > 0` on the deployed pool — this is governance-set via the `debtFloorInUsd`/`updateDebtFloor` path present in `contracts/Pool.sol` and the deployed `Pool.json` ABIs. I could not confirm the on-chain value from the index (deployment artifacts contain the ABI but not the live parameter), so the live-config precondition should be verified. The rest of the path (issue debt in the band, wait for unhealthy, all liquidations revert) is fully deterministic from contract code. Given the modest collateral needed to hold a floor-sized debt, cost of attack is low whenever the parameter is set.

### Recommendation
In `Pool.liquidate`, permit full repayment regardless of `maxLiquidable` when the account's total debt is below (or near) the floor — e.g., compute the cap as `max(maxLiquidable * debt, debt)` when `debtInUsd <= debtFloorInUsd`, or exempt `amountToRepay_ == _debtTokenBalance` from the `maxLiquidable` check so the floor can always be cleared. Alternatively skip the floor check when it leaves the position in a permanently unliquidatable band.

### Proof of Concept
Hardhat test sketch against `Pool.test.ts` fixtures:

```ts
// given: debtFloorInUsd set (e.g. governor calls updateDebtFloor to $100)
await pool.connect(governor).updateDebtFloor(toUSD('100'))
// alice deposits collateral and issues ~$150 debt (in dead zone: $100 < 150 < $200)
await msdMET.connect(alice).deposit(depositAmount, alice.address)
await msEthDebtToken.connect(alice).issue(debt150UsdInMsEth, alice.address)
// price moves so position is unhealthy
await masterOracle.updatePrice(met.address, lowerPrice)
expect((await pool.debtPositionOf(alice.address))._isHealthy).false

// liquidator tries full repay -> AmountGreaterThanMaxLiquidable
await expect(
  pool.connect(bob).liquidate(msEth.address, alice.address, fullDebt, msdMET.address)
).revertedWithCustomError(pool, 'AmountGreaterThanMaxLiquidable')

// liquidator tries 50% repay -> RemainingDebtIsLowerThanTheFloor
await expect(
  pool.connect(bob).liquidate(msEth.address, alice.address, halfDebt, msdMET.address)
).revertedWithCustomError(pool, 'RemainingDebtIsLowerThanTheFloor')
// => no amountToRepay_ succeeds; position is permanently unliquidatable
```

Caveat: the live value of `debtFloorInUsd` on each deployed pool could not be verified from the indexed artifacts; if it is `0` everywhere, the dead zone does not exist on current deployments and severity drops to a latent logic flaw.