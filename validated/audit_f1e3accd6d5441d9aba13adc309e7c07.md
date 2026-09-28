### Title
Unliquidatable positions when `debtFloorInUsd` and `maxLiquidable` interact — permanent liquidation DoS / bad debt - (File: contracts/Pool.sol)

### Summary
`Pool.liquidate` enforces two independent bounds that conflict: `amountToRepay_` cannot exceed `maxLiquidable` fraction of the debt (default 50%), and any nonzero post-liquidation debt below `debtFloorInUsd` reverts. For any position whose debt is worth less than `floor / maxLiquidable`, every allowed `amountToRepay_` reverts, so the position can never be liquidated. The bug class is a repeatable crash/hang of a critical operation (analogous to the MySQL optimizer DoS): a public call that deterministically reverts for a reachable state.

### Finding Description
In `Pool.liquidate` (contracts/Pool.sol:537-596):

1. `amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable` reverts — capping repayment at 50% of the debt (line 567-569, `maxLiquidable = 0.5e18` set in initialize at line 181).
2. The debt-floor check computes `_newDebtInUsd = quote(balance - amountToRepay_)` and reverts with `RemainingDebtIsLowerThanTheFloor` if `0 < _newDebtInUsd < debtFloorInUsd` (lines 571-579).

For a position with debt `D` (in USD), the only repayments that pass check 1 are `repay ≤ D * maxLiquidable`, leaving remainder `≥ D * (1 - maxLiquidable) = D/2`. Check 2 requires the remainder to be either `0` (i.e., `repay == D`, forbidden by check 1 whenever `maxLiquidable < 1`) or `≥ debtFloorInUsd`. Therefore, whenever `D/2 < debtFloorInUsd` (i.e., USD debt `< 2 * floor` with the default 50% cap), **every** `liquidate` call reverts.

An unprivileged attacker can open a position with debt sized just below `2 * debtFloorInUsd` via `DebtToken.issue`, then let it go underwater through normal price movement (or amplified by permitted same-transaction AMM moves on thin collateral). The position is then permanently unliquidatable by anyone. `quoteLiquidateMax` does not rescue this — it is capped by `maxLiquidable` too (lines 429-439), so it returns an amount that still reverts at the floor check or the `AmountIsTooHigh` check.

### Impact Explanation
Permanent freezing of the liquidation function for affected positions → guaranteed protocol bad debt / insolvency. Once an underwater position falls into the `debt < 2 * debtFloorInUsd` band (which is exactly the band most likely to matter, since the floor exists to keep liquidations economically viable), no liquidator can ever repay it: partial repays leave sub-floor debt, and full repay is blocked by `maxLiquidable`. The debt keeps accruing interest in `DebtToken` while the collateral can be fully drained, leaving unbacked synthetic supply.

### Likelihood Explanation
Requires `debtFloorInUsd > 0` (a deployed governance parameter) and `maxLiquidable < 1` (default 50%, hardcoded in `initialize`). Both conditions are the intended configuration — the floor exists specifically to bound liquidation sizes. An attacker needs only a public `issue` to create the position; no privileged role, oracle corruption, or malicious endpoint is required. Any underwater small-debt position (whether attacker-created or organic) enters the permanently-unliquidatable state.

### Recommendation
Inside `liquidate`, when the account's debt is below the floor (`_debtTokenBalance` worth `< debtFloorInUsd`), allow `amountToRepay_ == _debtTokenBalance` to bypass the `maxLiquidable` cap — i.e., permit full liquidation of small positions. Equivalently: `if (amountToRepay_ != _debtTokenBalance && amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) revert`. Alternatively, compute the cap as `max(maxLiquidable * debt, debt)` when the resulting remainder would be below the floor.

### Proof of Concept
Reproducible in the existing Hardhat suite (`test/Pool.test.ts`, `describe('debt floor')` at lines 408-437 shows both reverts independently but never combined):

```ts
// 1. Governor sets debtFloor = $3,000 (as in existing test).
await pool.updateDebtFloor(parseEther('3000'));
// maxLiquidable is 0.5e18 by default.

// 2. Attacker deposits collateral and issues debt D with $3,000 <= D < $6,000,
//    e.g. issue ~1.4 msETH at $4,000/ETH => ~$5,600 debt.
await msdMET.connect(attacker).deposit(collateral, attacker.address);
await msEthDebtToken.connect(attacker).issue(mintAmount, attacker.address);

// 3. Collateral price drops so the position is unhealthy.
await masterOracle.updatePrice(met.address, droppedPrice);

// 4. ANY liquidation attempt reverts:
//    - repay <= 50% of debt  => remainder ~$2,800 < $3,000 floor
//      => RemainingDebtIsLowerThanTheFloor
//    - repay = full debt     => repay/debt = 1 > 0.5
//      => AmountGreaterThanMaxLiquidable
await expect(
  pool.connect(liquidator).liquidate(msEth.address, attacker.address, halfDebt, msdMET.address)
).revertedWithCustomError(pool, 'RemainingDebtIsLowerThanTheFloor');
await expect(
  pool.connect(liquidator).liquidate(msEth.address, attacker.address, fullDebt, msdMET.address)
).revertedWithCustomError(pool, 'AmountGreaterThanMaxLiquidable');
// Position is permanently unliquidatable; protocol accrues bad debt.
```

Relevant code: `Pool.liquidate` bounds at [1](#0-0) , `maxLiquidable` default at [2](#0-1) , `quoteLiquidateMax` cap at [3](#0-2) , and existing floor tests at [4](#0-3) .

### Citations

**File:** contracts/Pool.sol (L181-181)
```text
        maxLiquidable = 0.5e18; // 50%
```

**File:** contracts/Pool.sol (L435-439)
```text
        _maxAmountToRepay = debtTokenOf[syntheticToken_].balanceOf(account_).wadMul(maxLiquidable);

        if (_amountToRepay < _maxAmountToRepay) {
            _maxAmountToRepay = _amountToRepay;
        }
```

**File:** contracts/Pool.sol (L567-579)
```text
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

**File:** test/Pool.test.ts (L408-436)
```typescript
          describe('debt floor', function () {
            it('should revert if debt becomes < debt floor', async function () {
              // given
              await pool.updateDebtFloor(parseEther('3000')) // $3,000
              const debtBefore = await msEthDebtToken.balanceOf(alice.address)
              expect(debtBefore).eq(parseEther('1')) // $4,000

              // when
              const amountToRepay = debtBefore.div('2') // $2,0000
              const tx = pool.connect(liquidator).liquidate(msEth.address, alice.address, amountToRepay, msdMET.address)

              // then
              await expect(tx).revertedWithCustomError(pool, 'RemainingDebtIsLowerThanTheFloor')
            })

            it('should allow erase debt when debt floor set', async function () {
              // given
              await pool.updateDebtFloor(parseEther('3000')) // $3,000
              const debtBefore = await msEthDebtToken.balanceOf(alice.address)
              expect(debtBefore).eq(parseEther('1')) // $4,000

              // when
              const amountToRepay = debtBefore
              await pool.connect(liquidator).liquidate(msEth.address, alice.address, amountToRepay, msdMET.address)

              // then
              const debtAfter = await msEthDebtToken.balanceOf(alice.address)
              expect(debtAfter).eq(0)
            })
```
