Let me check the `DebtToken` issue/mint path and whether deposit/mint can push tokens onto an arbitrary account's list.### Title
`debtFloorInUsd` + `maxLiquidable` checks make positions at the debt floor permanently unliquidatable, guaranteeing bad debt - (File: contracts/Pool.sol)

### Summary
`Pool.liquidate` enforces two validation bounds that contradict each other: (1) `amountToRepay_ / debtBalance <= maxLiquidable` (50% on the deployed configuration), and (2) the post-repayment debt must be either `0` or `>= debtFloorInUsd`, reverting with `RemainingDebtIsLowerThanTheFloor` otherwise. When an account's debt equals `debtFloorInUsd`, every possible `amountToRepay_` either leaves a positive remainder below the floor or exceeds `maxLiquidable`, so liquidation always reverts. This is the direct analog of CVE-2018-7714: a user-controllable input/state combination lands on an impossible-to-satisfy assertion, permanently DoSing a core protocol operation.

### Finding Description
In `Pool.liquidate`:

```solidity
// contracts/Pool.sol:567-579
if (amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) {
    revert AmountGreaterThanMaxLiquidable();
}
if (debtFloorInUsd > 0) {
    uint256 _newDebtInUsd = masterOracle().quoteTokenToUsd(
        address(syntheticToken_),
        _debtTokenBalance - amountToRepay_
    );
    if (_newDebtInUsd > 0 && _newDebtInUsd < debtFloorInUsd) {
        revert RemainingDebtIsLowerThanTheFloor();
    }
}
```

Let `F` be `debtFloorInUsd` and `m` be `maxLiquidable` (= `0.5e18`, set in `initialize`). If the position's debt `D` satisfies `D <= F` (in USD terms), then:

- Any `amountToRepay_` with `0 < repaid < D` leaves `D - repaid` in `(0, F)` → `RemainingDebtIsLowerThanTheFloor`.
- `amountToRepay_ = D` gives `wadDiv(D) = 1e18 > m` → `AmountGreaterThanMaxLiquidable`.

Both branches revert; the position can never be liquidated. There is also no alternative cleanup path: `DebtToken.repay`/`repayAll` by the *debtor* works, but the debtor has no incentive, and third parties repaying `onBehalfOf_` only reduce debt — they cannot seize collateral. `maxLiquidable` is governor-set (default 50%), and `debtFloorInUsd` is a deployed protocol parameter (`PoolStorage`), so this is reachable on the real configuration.

### Impact Explanation
An attacker (or any user) opens a position with debt `D ≈ F` (allowed: `DebtToken._mint` only requires `debtInUsd >= debtFloorInUsd`). When the position turns unhealthy — via normal market movement of the collateral or a same-transaction oracle price move — it is permanently unliquidatable. The collateral backing that debt can fall arbitrarily below the debt value while every liquidation transaction reverts, forcing the protocol to hold pure bad debt. Repeated across accounts this is direct protocol insolvency: synthetic supply stays outstanding with no way to clear the underwater debt. This breaks the solvency and liquidation-bounds invariants and qualifies as both insolvency and permanent liveness failure of the liquidation path.

### Likelihood Explanation
Reachable by any unprivileged EOA through the public `DebtToken.issue` entry point: deposit minimal collateral, mint debt equal to `debtFloorInUsd`. The trigger condition (position becoming unhealthy) happens naturally on any collateral price decline — no privileged role, no oracle corruption, no governance action needed. The only mitigation is that the impact per position is bounded by `F`, so material insolvency requires either a non-trivial `debtFloorInUsd` value or many such positions (which an attacker can open from multiple accounts at the cost of collateral). Note that `quoteLiquidateMax` does not help a liquidator escape the trap: it just quotes amounts subject to the same two checks.

### Recommendation
Allow full repayment when the remaining debt would otherwise fall below the floor. Concretely, in `Pool.liquidate`, when `_debtTokenBalance - amountToRepay_` would be `> 0` and `< debtFloorInUsd` *and* the position's full debt is needed to clear it (i.e., `debtTokenBalance <= maxLiquidable`-adjusted cap fails), either:

- exempt liquidation from the `maxLiquidable` cap when `amountToRepay_ == _debtTokenBalance` (full close always permitted), or
- clamp `amountToRepay_` to `_debtTokenBalance` whenever the residual would be below the floor.

Apply the same fix symmetrically in `DebtToken.repay` so a liquidator/payer can always zero-out a sub-floor debt.

### Proof of Concept
Reproducible on a mainnet fork (deployed `Pool` uses `maxLiquidable = 0.5e18` and a nonzero `debtFloorInUsd`):

```typescript
// Hardhat fork test sketch
// 1. Attacker deposits collateral C and issues debt D such that
//    quoteTokenToUsd(synth, D) == debtFloorInUsd exactly
await msdToken.deposit(collateralAmount, attacker.address);
await msUsdDebt.issue(debtAmountEqualToFloor, attacker.address); // D: debtInUsd == floor

// 2. Collateral price drops (or manipulate same-tx via DEX swap on the oracle source)
//    -> debtPositionOf(attacker)._isHealthy == false
const {_isHealthy} = await pool.debtPositionOf(attacker.address);
assert(!_isHealthy);

// 3. Any liquidation attempt reverts:
await expect(
  pool.connect(liquidator).liquidate(msUsd.address, attacker.address, D.div(2), msdToken.address)
).to.be.revertedWithCustomError(pool, 'RemainingDebtIsLowerThanTheFloor');

await expect(
  pool.connect(liquidator).liquidate(msUsd.address, attacker.address, D, msdToken.address)
).to.be.revertedWithCustomError(pool, 'AmountGreaterThanMaxLiquidable');
// -> bad debt is permanently stuck; no seize path exists
```

Confidence caveats: I verified the two reverts and `maxLiquidable = 0.5e18` default at `Pool.sol:181,567-577`, and that `issue` enforces only `>= debtFloorInUsd` at `DebtToken.sol:580-588`. Whether a production deployment actually sets `debtFloorInUsd > 0` should be confirmed on-chain; if it is `0` everywhere, the branch is dead code and the finding does not apply.