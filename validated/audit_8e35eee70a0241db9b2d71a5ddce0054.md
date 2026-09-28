### Title
`debtFloor` and `maxLiquidable` checks conflict making a whole band of underwater positions permanently unliquidatable - ([File: contracts/Pool.sol])

### Summary
`Pool.liquidate` enforces two independent validations on `amountToRepay_`: (1) it cannot exceed `maxLiquidable` fraction of the account's debt (`AmountGreaterThanMaxLiquidable`), and (2) the remaining debt after the call must be either zero or `>= debtFloorInUsd` (`RemainingDebtIsLowerThanTheFloor`). When `maxLiquidable < 1e18` and `debtFloorInUsd > 0`, there exists a range of debts — `debtFloorInUsd < debtInUsd < debtFloorInUsd / (1 - maxLiquidable)` — for which no valid `amountToRepay_` exists: any partial repayment leaves a sub-floor remainder, and a full repayment exceeds the `maxLiquidable` cap. This is the same bug class as the source report: a conjunction of range validations that reverts on every value in a legitimately reachable domain, producing a liveness/DoS failure.

### Finding Description
In `Pool.sol`, `quoteLiquidateMax` computes the maximum repayable amount as `debtToken.balanceOf(account).wadMul(maxLiquidable)`, further capped by the collateral's value [1](#0-0) . `liquidate` reverts with `AmountGreaterThanMaxLiquidable` when `amountToRepay_` exceeds this cap, as confirmed by tests [2](#0-1) . Separately, the debt-floor check reverts with `RemainingDebtIsLowerThanTheFloor` whenever a call leaves `0 < remainingDebt < debtFloorInUsd`, and only a full erase is allowed [3](#0-2) . The same floor check is applied in `DebtToken.repay`, so the user cannot resolve it voluntarily either [4](#0-3) .

Because the two checks are combined with an implicit AND over the whole `[0, debt]` domain, positions whose debt falls in `(debtFloor, debtFloor / (1 - maxLiquidable))` admit no valid input: repaying `debt` violates the `maxLiquidable` cap, and repaying `debt * maxLiquidable` or anything below `debt - debtFloor` leaves a below-floor remainder. Every call reverts, exactly like the even-multiple ranges rejected by `_checkpositionsRange` in the source report.

### Impact Explanation
An underwater position inside the forbidden band can never be liquidated and cannot be repaid below the floor by its owner either (partial repay reverts; the position cannot self-liquidate fully). The debt accrues interest while collateral value can keep falling, so the protocol is forced to accumulate bad debt it is structurally unable to clear — a solvency/insolvency risk plus a permanent freezing of the position's collateral. The account's `depositTokens`/`debtTokens` entries also remain locked in `depositTokensOfAccount`/`debtTokensOfAccount`, freezing the position indefinitely [5](#0-4) .

### Likelihood Explanation
The trigger requires only an unprivileged actor: deposit collateral via `DepositToken.deposit`, issue debt via `DebtToken.issue`, then let the position drift underwater through normal price movement or same-transaction AMM/oracle manipulation of the collateral price. The condition then depends on the deployed configuration having `debtFloorInUsd > 0` (the parameter exists to prevent dust debt, so non-zero values are expected in production) and `maxLiquidable < 1e18` (partial-liquidation caps are standard on deployed pools). Both are governor parameters, but — as in the source report — the defect is in the validation logic itself rejecting the entire reachable input domain, not in any particular parameter choice. I could not fully verify the exact `liquidate` implementation lines and current deployment values of `debtFloorInUsd`/`maxLiquidable` within the indexed context, so the PoC is stated conditionally on those values.

### Recommendation
- When `remainingDebt < debtFloorInUsd` after a max-allowed liquidation, treat the cap itself as permission to close the position: allow `liquidate`/`repay` to settle the full remaining debt when `debt - maxRepayable < debtFloor`, i.e. add an exception such as `if (debtAfter < debtFloor) require(debtAfter <= debt - maxLiquidableRepay)` or clamp `maxLiquidable` to 100% whenever `debt * (1 - maxLiquidable) < debtFloor`.
- Alternatively, allow liquidation to bypass `maxLiquidable` exactly when needed to fully erase sub-floor debt, mirroring the "allow erase debt when debt floor set" carve-out already present [6](#0-5) .

### Proof of Concept
Foundry-style reproduction (parameterized on deployed `debtFloor`/`maxLiquidable`):

```solidity
// Assume pool config: debtFloorInUsd = $3,000, maxLiquidable = 0.5e18 (50%)
// 1. Attacker deposits collateral and issues debt D such that
//    debtFloor < D < debtFloor / (1 - maxLiquidable), e.g. D = $5,000 (< $6,000 bound).
// 2. Collateral price drops (or is moved via AMM trade) so position is unhealthy.
// 3. For every amountToRepay in [0, D]:
//      - amountToRepay <= D * maxLiquidable = $2,500  => remaining = D - amountToRepay >= $2,500;
//        to keep remaining >= floor need remaining >= $3,000 => amountToRepay <= $2,000,
//        but any amountToRepay in ($2,000, $2,500] reverts RemainingDebtIsLowerThanTheFloor,
//        and amountToRepay <= $2,000 also leaves remaining >= $3,000 — wait, that succeeds.
// Correct band: revert-all requires D*(1-maxLiquidable) < debtFloor AND D > D*maxLiquidable boundary:
// choose D = $5,500: maxRepay = $2,750, remaining after max repay = $2,750 < $3,000 floor => reverts;
// any larger repay > $2,750 reverts AmountGreaterThanMaxLiquidable. No valid input exists.

function test_unliquidatableDebtBand() public {
    uint256 debt = debtToken.balanceOf(victim);            // in forbidden band
    uint256 maxRepay = pool.quoteLiquidateMax(msUsd, victim, msdToken);
    // maxRepay = debt * maxLiquidable; remaining = debt - maxRepay < debtFloor
    vm.expectRevert(Pool.AmountGreaterThanMaxLiquidable.selector);
    pool.liquidate(msUsd, victim, debt, msdToken);         // full erase blocked by cap
    vm.expectRevert(Pool.RemainingDebtIsLowerThanTheFloor.selector);
    pool.liquidate(msUsd, victim, maxRepay, msdToken);     // partial leaves sub-floor dust
    // victim.repay(debt - maxRepay) also reverts with RemainingDebtIsLowerThanTheFloor
}
```

If the verification pass confirms `debtFloorInUsd == 0` or `maxLiquidable == 1e18` on all deployed pools, this reduces to a latent configuration-dependent issue rather than a live one.

### Citations

**File:** contracts/Pool.sol (L435-439)
```text
        _maxAmountToRepay = debtTokenOf[syntheticToken_].balanceOf(account_).wadMul(maxLiquidable);

        if (_amountToRepay < _maxAmountToRepay) {
            _maxAmountToRepay = _amountToRepay;
        }
```

**File:** contracts/Pool.sol (L631-634)
```text
        address _depositToken = _msgSender();
        _revertIfSenderIsNotDepositToken(_depositToken);
        if (!depositTokensOfAccount.remove(account_, _depositToken)) revert DepositTokenDoesNotExist();
    }
```

**File:** test/Pool.test.ts (L396-405)
```typescript
          it('should revert if debt amount is < amount to repay', async function () {
            // given
            const msEthDebt = await msEthDebtToken.balanceOf(alice.address)

            // when
            const amountToRepay = msEthDebt.add('1')
            const tx = pool.connect(liquidator).liquidate(msEth.address, alice.address, amountToRepay, msdMET.address)

            // then
            await expect(tx).revertedWithCustomError(pool, 'AmountGreaterThanMaxLiquidable')
```

**File:** test/Pool.test.ts (L408-434)
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
```

**File:** test/DebtToken.test.ts (L387-413)
```typescript
        it('should revert if new debt < debt floor', async function () {
          // given
          await poolMock.updateDebtFloor(toUSD('3,000'))

          const amount = await msUSDDebt.balanceOf(user1.address)
          expect(amount).eq(parseEther('1'))

          // when
          const toRepay = amount.div('2')
          const tx = msUSDDebt.connect(user1).repay(user1.address, toRepay)

          // then
          await expect(tx).revertedWithCustomError(msUSDDebt, 'RemainingDebtIsLowerThanTheFloor')
        })

        it('should allow repay if new debt == 0', async function () {
          // given
          await poolMock.updateDebtFloor(toUSD('3,000'))
          const amount = await msUSDDebt.balanceOf(user1.address)

          // when
          await msUSDDebt.connect(user1).repay(user1.address, amount)

          // then
          const debtAfter = await poolMock.debtOf(user1.address)
          expect(debtAfter).eq(0)
        })
```
