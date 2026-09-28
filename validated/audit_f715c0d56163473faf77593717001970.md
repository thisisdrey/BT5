### Title
Attacker-crafted dust position makes `Pool.liquidate` permanently revert, freezing bad debt in the protocol - (File: contracts/Pool.sol)

### Summary
`Pool.liquidate` reverts unconditionally whenever the quoted collateral to seize exceeds the account's `DepositToken` balance (`AmountIsTooHigh`), and reverts again whenever the post-liquidation remaining debt lands below `debtFloorInUsd` (`RemainingDebtIsLowerThanTheFloor`). An unprivileged attacker can engineer a position whose collateral is exactly at (or fractionally below) the seizeable quote for the max-liquidatable debt fraction, so that every call to `liquidate` reverts. The analog to CVE-2016-10371 is a hard "assert" on crafted input: the attacker chooses the position shape, and the revert kills the liquidation path rather than the attacker — permanently freezing bad debt inside the protocol.

### Finding Description
In `Pool.liquidate` the protocol performs two order-sensitive checks before burning debt and seizing collateral:

1. `amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable` caps the repayable fraction [1](#0-0) 
2. If `debtFloorInUsd > 0`, repaying an amount that leaves `0 < newDebt < debtFloorInUsd` reverts [2](#0-1) 
3. `quoteLiquidateOut` is then called and `_totalSeized > depositToken_.balanceOf(account_)` reverts with `AmountIsTooHigh` [3](#0-2) 

`WadRayMath.wadMul`/`wadDiv` round half-up, so when `amountToRepay` approaches the position's full debt and the USD-denominated incentive is applied, the computed `_totalSeized` can exceed the account's exact deposit balance by 1 wei. Because `maxLiquidable` divides the repayable amount and `debtFloorInUsd` forbids leaving a small remainder, an attacker can open a position (deposit + `DebtToken.issue`), manipulate pool/AMM prices within the same transaction to push it just underwater, and size the collateral so that:

- any `amountToRepay` within `maxLiquidable` leaves `newDebtInUsd ∈ (0, debtFloorInUsd)` → `RemainingDebtIsLowerThanTheFloor`, or
- the rounding-up `_totalSeized` exceeds `balanceOf` → `AmountIsTooHigh`.

Every external `liquidate` call then reverts deterministically. There is no alternative cleanup path: `DebtToken.repay` applies the identical floor check [4](#0-3) , and `repayAll` requires the caller to hold the full synthetic amount, which rational liquidators won't do for an unprofitable dust position.

### Impact Explanation
Permanent freezing of funds / protocol insolvency: the bad debt can never be liquidated, and the trapped collateral is economically locked behind an un-repayable debt position. As the oracle price continues to move against the position, the gap between debt and collateral grows into realized bad debt borne by the protocol and other synthetic holders. This matches the accepted impact classes (permanent freezing of funds, protocol insolvency).

### Likelihood Explanation
Medium. Triggering requires `debtFloorInUsd > 0` or a seize-quote boundary, both of which are live configuration values, and an unhealthy position the attacker can create with ordinary `DepositToken.deposit` + `DebtToken.issue` calls plus same-transaction price movement (allowed by scope). No privileged role is needed for creation or for demonstrating the revert — any EOA can reproduce the revert by calling `liquidate` on the crafted account.

### Recommendation
- In `liquidate`, clamp `_totalSeized` to `depositToken_.balanceOf(account_)` instead of reverting when the difference is within rounding dust, or revert only above a dust threshold.
- When `debtFloorInUsd > 0`, allow liquidation (and `repay`) to erase the full debt even when `maxLiquidable < 100%` if the remainder would fall below the floor — i.e., check `newDebtInUsd < debtFloor` only when the caller is not fully clearing the debt, or auto-promote `amountToRepay` to `balanceOf` in that case.
- Add fork tests exercising liquidations of minimum-size unhealthy positions at the `maxLiquidable`/`debtFloorInUsd` boundary.

### Proof of Concept
Hardhat fork sketch (optimism deployment in `deployments/`):

```ts
// 1. Deploy/attach Pool, DepositToken (msdMET), DebtToken (msETH debt), MasterOracle on fork.
// 2. Governance state assumed: debtFloorInUsd = $X > 0, maxLiquidable = 50% (as in deployed config).
// 3. Attacker:
await msdMET.deposit(collateral, attacker.address);           // sized so debt*2*incentive ~= balance
await msEthDebtToken.issue(debtAmount, attacker.address);     // debt USD value slightly > debtFloor
// 4. Same-tx price push (or natural drift) -> position becomes unhealthy.
// 5. Any liquidator EOA:
await expect(
  pool.connect(liq).liquidate(msETH, attacker.address, debt.mul(maxLiquidable).div(1e18), msdMET)
).to.be.revertedWithCustomError(pool, 'RemainingDebtIsLowerThanTheFloor');
//    repaying more reverts with 'AmountGreaterThanMaxLiquidable';
//    repaying exactly balance reverts with 'AmountIsTooHigh' when wadMul rounds seize quote 1 wei over balance.
// Result: no amountToRepay value succeeds -> position unliquidatable, bad debt frozen.
```

Reference test harness exists at `test/Pool.test.ts` (debt-floor and max-liquidable revert cases at lines ~408–451) which already demonstrates the individual reverts; the PoC composes them on one crafted position. [5](#0-4) [6](#0-5)

### Citations

**File:** contracts/Pool.sol (L553-569)
```text
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
```

**File:** contracts/Pool.sol (L571-578)
```text
        if (debtFloorInUsd > 0) {
            uint256 _newDebtInUsd = masterOracle().quoteTokenToUsd(
                address(syntheticToken_),
                _debtTokenBalance - amountToRepay_
            );
            if (_newDebtInUsd > 0 && _newDebtInUsd < debtFloorInUsd) {
                revert RemainingDebtIsLowerThanTheFloor();
            }
```

**File:** contracts/Pool.sol (L581-585)
```text
        (_totalSeized, _toLiquidator, _fee) = quoteLiquidateOut(syntheticToken_, amountToRepay_, depositToken_);

        if (_totalSeized > depositToken_.balanceOf(account_)) {
            revert AmountIsTooHigh();
        }
```

**File:** contracts/DebtToken.sol (L442-450)
```text
        uint256 _debtFloorInUsd = _pool.debtFloorInUsd();
        if (_debtFloorInUsd > 0) {
            uint256 _newDebtInUsd = _pool.masterOracle().quoteTokenToUsd(
                address(_syntheticToken),
                balanceOf(onBehalfOf_) - _repaid
            );
            if (_newDebtInUsd > 0 && _newDebtInUsd < _debtFloorInUsd) {
                revert RemainingDebtIsLowerThanTheFloor();
            }
```

**File:** test/Pool.test.ts (L408-451)
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
          })

          it('should revert if repaying more than max allowed to liquidate', async function () {
            // given
            const maxLiquidable = parseEther('0.5') // 50%
            await pool.updateMaxLiquidable(maxLiquidable)
            const msEthDebt = await msEthDebtToken.balanceOf(alice.address)

            // when
            const amountToRepay = msEthDebt.div('2').add('1')
            const tx = pool.connect(liquidator).liquidate(msEth.address, alice.address, amountToRepay, msdMET.address)

            // then
            await expect(tx).revertedWithCustomError(pool, 'AmountGreaterThanMaxLiquidable')
          })
```
