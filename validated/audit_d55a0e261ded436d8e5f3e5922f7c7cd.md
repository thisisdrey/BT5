### Title
Dust debt positions below `2 * debtFloorInUsd` can never be liquidated, permanently blocking `Pool.liquidate` and accruing bad debt - ([File: contracts/Pool.sol])

### Summary
The Squid advisory is a use-after-free DoS reachable by an unprivileged client that poisons shared request processing. The Metronome analog is a liquidation-liveness DoS: the interaction between `maxLiquidable` (a hard cap of 50% repay per call) and `debtFloorInUsd` (a minimum remaining-debt floor) makes any debt position smaller than `2 * debtFloorInUsd` permanently unliquidatable. An unprivileged attacker can open such dust positions cheaply; once they become unhealthy, no liquidator can ever clear them, and interest accrual turns them into realized bad debt.

### Finding Description
`Pool.liquidate` enforces two constraints on `amountToRepay_`:

1. The repay amount cannot exceed `maxLiquidable` (initialized to `0.5e18`, i.e. 50%) of the account's debt-token balance: `if (amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) revert AmountGreaterThanMaxLiquidable();` [1](#0-0) 

2. The remaining debt after repayment must be either `0` or `>= debtFloorInUsd`: `if (_newDebtInUsd > 0 && _newDebtInUsd < debtFloorInUsd) revert RemainingDebtIsLowerThanTheFloor();` [2](#0-1) 

These checks are mutually exclusive for small positions. For an account with `debtBalance` whose USD value `D` satisfies `0 < D < 2 * debtFloorInUsd`:

- Any full repayment (`amountToRepay_ = debtBalance`, making `_newDebtInUsd = 0`) requires `wadDiv = 1e18 > maxLiquidable (0.5e18)` → reverts with `AmountGreaterThanMaxLiquidable`.
- Any partial repayment allowed by the cap (`amountToRepay_ <= 0.5 * debtBalance`) leaves `remaining >= 0.5 * debtBalance > 0`, and since `D < 2 * debtFloorInUsd`, `remaining < debtFloorInUsd` → reverts with `RemainingDebtIsLowerThanTheFloor`.

Every possible `amountToRepay_` reverts, so `liquidate` can never succeed for the position. [3](#0-2) 

The attacker path is fully unprivileged: `DepositToken.deposit` → `DebtToken.issue` a dust amount of a synthetic such that its USD value lands just under `2 * debtFloorInUsd` (a bound known on-chain via `pool.debtFloorInUsd()`). No governor, keeper, or oracle manipulation is required — the position goes unhealthy either through normal price movement or because accrued interest (`DebtToken.accrueInterest`, called at the top of `liquidate`) pushes `debtInUsd > issuableLimitInUsd`. [4](#0-3) 

### Impact Explanation
Liquidation is the only mechanism that converts unhealthy positions back to solvency. A position in the dead zone can never be liquidated:

- While it remains unhealthy but collateral-backed, the protocol carries a position no one can close.
- As `DebtToken` interest accrues, `debtInUsd` keeps growing while the seized collateral value stays fixed. When `debtInUsd` eventually exceeds `2 * debtFloorInUsd` the position becomes "liquidatable" again — but by then the collateral is worth less than the debt, so a liquidator seizing `depositToken.balanceOf(account)` does not cover `debtToken.balanceOf(account)`. The residual debt is burned against collateral that no longer exists, i.e. realized bad debt / protocol insolvency.
- The attacker can multiply this across many accounts/tokens (each costs only the dust collateral), amplifying aggregate bad debt.

This breaks the solvency/liquidation-bounds invariant and satisfies the "permanent freezing of funds / protocol insolvency" acceptance criterion (the collateral backing the dust debt is permanently trapped; the debt becomes unbacked).

### Likelihood Explanation
- Attacker is unprivileged: only `deposit` and `issue` via public entry points (or `Operator.execute`).
- Cost is bounded by `2 * debtFloorInUsd` of collateral per position plus gas; `debtFloorInUsd` is intended to be small (a "dust" floor), so the attack is cheap.
- No privileged dependency, no oracle correctness assumption — interest accrual alone (`debtIndex` grows every block via `accrueInterest`) eventually makes a minimally-collateralized position unhealthy.
- Not blocked by modifiers: `liquidate`'s `whenNotShutdown`, `nonReentrant`, and health check all pass; the revert is intrinsic to the math. `SynthContext`/`getActualMsgSender` does not alter the bounds.

### Recommendation
Allow liquidation of positions whose remaining debt would fall below the floor, e.g.:

- Exempt full-seizure liquidations: if `amountToRepay_` repays the entire balance of that debt token, skip the `RemainingDebtIsLowerThanTheFloor` check **and** skip/relax the `maxLiquidable` cap when `depositToken.balanceOf(account_)` would be fully seized anyway; or
- Permit `maxLiquidable` override when `_debtTokenBalance`'s USD value is below `2 * debtFloorInUsd`, so dust positions are always fully closeable; or
- Enforce `debtFloorInUsd` at issuance time strictly (already partially done) **and** auto-allow full liquidation when `debt < floor`, treating floor positions as always liquidatable-in-full regardless of `maxLiquidable`.

The cleanest fix is: when `amountToRepay_ == _debtTokenBalance`, bypass both the `maxLiquidable` and the floor check.

### Proof of Concept
Hardhat/fork sketch (contracts are already deployed per `deployments/`; use a mainnet/Base fork or the existing test fixture):

```ts
// Pool.debtFloorInUsd() = F > 0, maxLiquidable = 0.5e18 (Pool.sol:181)
// 1. Attacker deposits collateral C (e.g. via DepositToken.deposit).
await depositToken.deposit(collateralAmount, attacker.address);

// 2. Attacker issues dust debt D such that F < D_usd < 2*F.
//    debtAmount chosen via masterOracle.quoteUsdToToken(synth, 1.5 * F)
await debtToken.issue(debtAmount, attacker.address);

// 3. Position becomes unhealthy: withdraw collateral down to the
//    issuable limit, or let interest accrue until
//    pool.debtPositionOf(attacker).isHealthy == false.

// 4. Any liquidator call reverts:
//    - full repay:  AmountGreaterThanMaxLiquidable (1e18 > 0.5e18)
//    - 50% repay:   RemainingDebtIsLowerThanTheFloor (0.75*F in (0, F))
await expect(
  pool.connect(liquidator).liquidate(synth, attacker.address, debtAmount, depositToken)
).to.be.revertedWithCustomError(pool, 'AmountGreaterThanMaxLiquidable');

await expect(
  pool.connect(liquidator).liquidate(synth, attacker.address, debtAmount / 2, depositToken)
).to.be.revertedWithCustomError(pool, 'RemainingDebtIsLowerThanTheFloor');

// Binary search over amountToRepay_ in (0, debtAmount] shows every value
// reverts: liquidatable set is empty while 0 < debtUsd < 2 * debtFloorInUsd.
```

Reproduction note: the revert set is exhaustive — `amountToRepay_` must satisfy `amountToRepay_ <= 0.5 * debtBalance` (else `AmountGreaterThanMaxLiquidable`) and `remaining == 0 || remainingUsd >= F` simultaneously; for `D < 2F` the only candidate satisfying the floor constraint is `remaining = 0`, which requires `amountToRepay_ = debtBalance > 0.5 * debtBalance`. No valid input exists, confirming permanent liquidation DoS for any position opened below `2 * debtFloorInUsd`.

### Citations

**File:** contracts/Pool.sol (L181-181)
```text
        maxLiquidable = 0.5e18; // 50%
```

**File:** contracts/Pool.sol (L537-596)
```text
    function liquidate(
        ISyntheticToken syntheticToken_,
        address account_,
        uint256 amountToRepay_,
        IDepositToken depositToken_
    )
        external
        override
        whenNotShutdown
        nonReentrant
        onlyIfSyntheticTokenExists(syntheticToken_)
        onlyIfDepositTokenExists(depositToken_)
        returns (uint256 _totalSeized, uint256 _toLiquidator, uint256 _fee)
    {
        address _msgSender = _msgSender();

        if (amountToRepay_ == 0) revert AmountIsZero();
        if (_msgSender == account_) revert CanNotLiquidateOwnPosition();

        IDebtToken _debtToken = debtTokenOf[syntheticToken_];
        _debtToken.accrueInterest();

        (bool _isHealthy, , , , ) = debtPositionOf(account_);

        if (_isHealthy) {
            revert PositionIsHealthy();
        }

        uint256 _debtTokenBalance = _debtToken.balanceOf(account_);

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

        (_totalSeized, _toLiquidator, _fee) = quoteLiquidateOut(syntheticToken_, amountToRepay_, depositToken_);

        if (_totalSeized > depositToken_.balanceOf(account_)) {
            revert AmountIsTooHigh();
        }

        syntheticToken_.burn(_msgSender, amountToRepay_);
        _debtToken.burn(account_, amountToRepay_);
        depositToken_.seize(account_, _msgSender, _toLiquidator);

        if (_fee > 0) {
            depositToken_.seize(account_, _poolRegistry.feeCollector(), _fee);
        }

        emit PositionLiquidated(_msgSender, account_, syntheticToken_, amountToRepay_, _totalSeized, _fee);
    }
```
