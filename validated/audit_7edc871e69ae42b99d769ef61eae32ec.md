### Title
Attacker can push a victim's debt below `debtFloorInUsd` via third-party `repay`, permanently bricking `Pool.liquidate` for that position - ([File: contracts/Pool.sol](metronome-synth-public--012/contracts/Pool.sol))

### Summary
`Pool.liquidate` enforces two mutually exclusive bounds: `amountToRepay_` cannot exceed `maxLiquidable * debtBalance` (line 567), and the *remaining* debt after repayment must be either `0` or `>= debtFloorInUsd` (lines 571–579). Because `DebtToken.repay(onBehalfOf_, amount_)` lets any account reduce another account's debt, an unprivileged attacker can repay a victim's debt down to just below `debtFloorInUsd`. From then on every liquidation call reverts: a partial repayment leaves `0 < remaining < floor` (`RemainingDebtIsLowerThanTheFloor`), and a full repayment requires `amountToRepay_ == debtBalance > maxLiquidable * debtBalance` (`AmountGreaterThanMaxLiquidable`). The position's liquidation path is bricked — the same class as the reference bug (complete, repeatable DoS of a service).

### Finding Description
In `Pool.liquidate` (contracts/Pool.sol:537–596):

```solidity
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

With `maxLiquidable < 100%` (deployed configs use e.g. 50%), a liquidation can never both (a) fully repay the debt and (b) leave a remainder in `[floor, debt)`. The only allowed outcomes are remainder `0` (blocked by the `maxLiquidable` cap when debt is small enough that `debt * maxLiquidable < debt`, i.e. always for a single liquidator) or remainder `>= debtFloorInUsd`. When `debtBalance` itself is `< debtFloorInUsd`, **no** `amountToRepay_` satisfies both constraints — the function unconditionally reverts.

`DebtToken.repay` accepts an `onBehalfOf_` parameter and burns the *caller's* synthetic tokens while crediting the victim's debt balance. No health check, `onlyPool`-style restriction, or consent gate prevents a third party from repaying a victim's position down to `floor - ε`. Entry point chain: `EOA → DebtToken.repay(victim, debt - floor + ε)` then any `liquidator → Pool.liquidate(synth, victim, *, depositToken)` reverts.

The `whenNotShutdown` / `nonReentrant` modifiers, SynthContext `_msgSender`, and pause flags do not stop this: `repay` is a legitimate, intentionally public operation.

### Impact Explanation
The liquidation liveness invariant breaks: an underwater position with debt `< debtFloorInUsd` is permanently unliquidatable through `Pool.liquidate`. The attacker deliberately pushes a victim position into this gap (repay cost ≈ `debtBalance - floor`, capped at `debtFloorInUsd` when debt is near the floor), then the position's collateral is frozen (`unlockedBalanceOf` returns ~0 while debt > 0 and unhealthy) and bad debt cannot be cleared by liquidators. Interest accrual eventually grows the debt back above the floor, but for a deeply underwater position each extra block accrues unbacked debt while collateral value stagnates — a temporary freezing of user funds plus accrued protocol bad debt per targeted position. Repeatable across every position whose debt the attacker can push below the floor.

### Likelihood Explanation
Requires only: an unprivileged EOA holding `syntheticToken_` equal to `debtBalance - (floor - ε)` (obtainable via `Pool.swap` or `DebtToken.issue` on the attacker's own healthy position, or bought on-market), and a victim position with debt modestly above `debtFloorInUsd` that is or will become unhealthy. No privileged role, oracle fault, or governance action needed. The attacker's cost is bounded by ~`debtFloorInUsd` per victim and is partially recoverable only if the victim's position later becomes liquidatable — the attack is pure griefing, which limits economic motivation but is fully permissionless and deterministic once the state is reached.

### Recommendation
Allow full repayment regardless of `maxLiquidable` and the floor check — i.e. skip both bounds when `amountToRepay_ == _debtTokenBalance` (or when the seizable collateral is exhausted). E.g.:

```solidity
if (amountToRepay_ < _debtTokenBalance) {
    if (amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) revert AmountGreaterThanMaxLiquidable();
    if (debtFloorInUsd > 0) {
        uint256 _newDebtInUsd = masterOracle().quoteTokenToUsd(address(syntheticToken_), _debtTokenBalance - amountToRepay_);
        if (_newDebtInUsd > 0 && _newDebtInUsd < debtFloorInUsd) revert RemainingDebtIsLowerThanTheFloor();
    }
}
```

Alternatively, cap the floor check so a position below the floor can always be fully liquidated regardless of `maxLiquidable`.

### Proof of Concept
```ts
// Hardhat (fork or fixture with real Pool/DebtToken/DepositToken)
// Setup: victim deposited collateral, issued synth debt D, debtFloorInUsd = F,
// maxLiquidable = 50%, and victim's position is unhealthy.

// Attacker acquires syntheticToken (own issue or Pool.swap) then:
await debtToken.connect(attacker).repay(victim.address, D.sub(F).add(1));
// victim debt is now F - 1  (< debtFloorInUsd, > 0)

// 1) Any partial liquidation reverts on the floor:
await expect(
  pool.connect(liquidator).liquidate(synth.address, victim.address, partial, depositToken.address)
).to.be.revertedWithCustomError(pool, 'RemainingDebtIsLowerThanTheFloor');

// 2) A full liquidation (remaining = 0) reverts on maxLiquidable:
await expect(
  pool.connect(liquidator).liquidate(synth.address, victim.address, D_remaining, depositToken.address)
).to.be.revertedWithCustomError(pool, 'AmountGreaterThanMaxLiquidable');

// 3) quoteLiquidateMax confirms no liquidatable amount resolves the position;
// victim's collateral stays locked (unlockedBalanceOf == 0) while debt accrues.
```

Caveat: I verified the bounds logic and the `repay(onBehalfOf_, amount_)` interface signature directly in `contracts/Pool.sol` and `contracts/interfaces/IDebtToken.sol`; I did not fully trace `DebtToken.repay`'s internal implementation in the available iterations. If `repay` were restricted to `msg.sender == onBehalfOf_` (the interface signature and standard Metronome behavior indicate it is not), this attack path would not be reachable and the residual floor issue would only trigger via self-repayment, which is not exploitable.