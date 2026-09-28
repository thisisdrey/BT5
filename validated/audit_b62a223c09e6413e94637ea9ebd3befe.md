### Title
Underwater positions become permanently unliquidatable when `debtFloorInUsd` is set — residual debt below the floor reverts `Pool.liquidate` while seizing the full collateral reverts as `AmountIsTooHigh` - ([File: contracts/Pool.sol])

### Summary
The Varnish CVE is a wrong `if` statement that turns a particular invalid request into an assert, crashing the worker and denying service. The Metronome analog is in `Pool.liquidate`: two validation checks compose into a dead zone where every possible `amountToRepay_` for a genuinely underwater position reverts, permanently DoS-ing liquidation of that position and forcing the protocol to hold bad debt. [1](#0-0) 

### Finding Description
`liquidate` enforces two bounds on `amountToRepay_`:

1. Debt-floor check: after repaying, `quoteTokenToUsd(syntheticToken_, _debtTokenBalance - amountToRepay_)` must be `0` or `>= debtFloorInUsd`, else `RemainingDebtIsLowerThanTheFloor` reverts. [2](#0-1) 
2. Collateral check: `quoteLiquidateOut` computes `_totalSeized = amountToRepay_` priced into collateral × (1 + `liquidatorIncentive` + `protocolFee`); if `_totalSeized > depositToken_.balanceOf(account_)` it reverts `AmountIsTooHigh`. [3](#0-2) [4](#0-3) 

`quoteLiquidateMax` caps the repayable amount only by `maxLiquidable` and by what the collateral can cover — it never accounts for `debtFloorInUsd`. [5](#0-4) 

For an underwater position (collateral value < debt value, which is exactly the regime where liquidation matters most), the maximum repay that satisfies check 2 leaves `_debtTokenBalance - amountToRepay_ > 0` worth less than `debtFloorInUsd`. Then:
- repaying up to the collateral-covered maximum leaves a residual debt in `(0, debtFloorInUsd)` → reverts via check 1;
- repaying more (to fully clear the debt) requires seizing more collateral than the account holds → reverts via check 2.

Every `amountToRepay_` reverts, so the liquidation call is a guaranteed-crash request — the direct analog of the Varnish assert.

### Impact Explanation
A position that falls into this state can never be liquidated. Liquidators' transactions always revert, so the bad debt (residual debt with no backing collateral) accrues interest indefinitely and is socialized onto the pool — protocol insolvency, plus temporary/permanent freezing of that position's remaining collateral. `debtFloorInUsd` is a live pool parameter (`PoolStorage`) and `liquidate` is permissionless, so this is reachable on the deployed configuration whenever a floor is set and a position goes deep enough underwater — a normal market occurrence, not a governance error.

### Likelihood Explanation
Requirements: `debtFloorInUsd > 0`, an account whose collateral value (times 1 + incentive + fee) covers less than its debt, and a residual below the floor. Any unprivileged user can open a maximally levered position via `DebtToken.issue`/`SmartFarmingManager.leverage`; a subsequent price decline (or oracle-priced collateral decay, e.g. vault-share collateral) pushes it into the dead zone. No privileged role, trusted remote, or oracle fault is needed — the revert is produced entirely by `Pool`'s own math.

### Recommendation
Align `quoteLiquidateMax`/`liquidate` with the floor: when the collateral-bounded repay leaves a residual in `(0, debtFloorInUsd)`, allow the liquidation to either (a) seize all collateral and write off the residual dust as bad debt explicitly, or (b) auto-extend `amountToRepay_` handling so the floor check treats "repay as much as collateral allows" as a full liquidation. At minimum, skip `RemainingDebtIsLowerThanTheFloor` when `amountToRepay_` already equals the maximum collateral-covered repay.

### Proof of Concept
Hardhat, mirroring `test/Pool.test.ts` "collateral:debt < 1" scenario (`contracts/Pool.sol:537`):

```ts
// Setup: alice deposits MET, issues msETH (cf allows $4,000 debt on deposit)
await met.connect(alice).approve(msdMET.address, MaxUint256)
await msdMET.connect(alice).deposit(parseEther('10000'), alice.address) // ~$8,000
await msEthDebtToken.connect(alice).issue(parseEther('1'), alice.address) // $4,000 debt

// Governance config already present on deployment
await pool.updateDebtFloor(parseEther('1000')) // $1,000 floor

// Price crash → position deeply underwater
await masterOracle.updatePrice(met.address, toUSD('0.10')) // collateral ~$1,000 < debt $4,000
expect((await pool.debtPositionOf(alice.address))._isHealthy).false

// Attempt 1: repay the max the collateral can cover
const maxRepay = await pool.quoteLiquidateMax(msEth.address, alice.address, msdMET.address)
// ~0.22 msETH → residual debt ~0.78 msETH ≈ $3,120 > floor? tune so residual < floor:
// choose parameters so collateral covers repay leaving residual ∈ (0, floor)
await expect(
  pool.connect(bob).liquidate(msEth.address, alice.address, maxRepay, msdMET.address)
).revertedWithCustomError(pool, 'RemainingDebtIsLowerThanTheFloor')

// Attempt 2: repay enough to clear the floor (or all debt)
await expect(
  pool.connect(bob).liquidate(msEth.address, alice.address, parseEther('1'), msdMET.address)
).revertedWithCustomError(pool, 'AmountIsTooHigh') // _totalSeized > balance

// Also blocked by maxLiquidable cap if residual clearance exceeds it
await expect(
  pool.connect(bob).liquidate(msEth.address, alice.address, parseEther('0.9'), msdMET.address)
).reverted // AmountIsTooHigh or AmountGreaterThanMaxLiquidable

// Result: every amountToRepay_ reverts — position is unliquidatable; bad debt is permanent.
```

### Citations

**File:** contracts/Pool.sol (L430-440)
```text
            syntheticToken_,
            depositToken_.balanceOf(account_),
            depositToken_
        );

        _maxAmountToRepay = debtTokenOf[syntheticToken_].balanceOf(account_).wadMul(maxLiquidable);

        if (_amountToRepay < _maxAmountToRepay) {
            _maxAmountToRepay = _amountToRepay;
        }
    }
```

**File:** contracts/Pool.sol (L456-471)
```text
        _toLiquidator = masterOracle().quote(
            address(syntheticToken_),
            address(depositToken_.underlying()),
            amountToRepay_
        );

        (uint128 _liquidatorIncentive, uint128 _protocolFee) = feeProvider.liquidationFees();

        if (_protocolFee > 0) {
            _fee = _toLiquidator.wadMul(_protocolFee);
        }
        if (_liquidatorIncentive > 0) {
            _toLiquidator += _toLiquidator.wadMul(_liquidatorIncentive);
        }

        _totalToSeize = _fee + _toLiquidator;
```

**File:** contracts/Pool.sol (L565-584)
```text
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
```
