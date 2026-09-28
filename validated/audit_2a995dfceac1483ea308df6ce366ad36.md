### Title
`Pool.liquidate` dead-zone: debt between `debtFloorInUsd` and `debtFloorInUsd / (1 - maxLiquidable)` can never be liquidated, permanently freezing liquidation and accruing bad debt - (File: contracts/Pool.sol)

### Summary
`Pool.liquidate` enforces two checks that are individually legitimate but jointly create an unreachable liquidation region:

1. `amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable` reverts (`AmountGreaterThanMaxLiquidable`), so a liquidator can never repay the full debt when `maxLiquidable < 100%` — the "repay everything, residual = 0" escape hatch is closed (contracts/Pool.sol:567). [1](#0-0) 
2. Any partial repayment that leaves `0 < newDebtInUsd < debtFloorInUsd` reverts (`RemainingDebtIsLowerThanTheFloor`) (contracts/Pool.sol:571-579). [2](#0-1) 

For an unhealthy position with debt token balance `D` and USD value `V(D)`, every admissible `amountToRepay_` satisfies `amountToRepay_ <= D * maxLiquidable`, hence residual debt `>= V(D) * (1 - maxLiquidable)`. When `V(D)` is in `[debtFloorInUsd, debtFloorInUsd / (1 - maxLiquidable))`, all partial repays violate the floor and full repay violates `maxLiquidable` → `liquidate` always reverts. This mirrors the Xen bug class: a combination of states that are each legitimate (capped liquidation size + dust-debt floor + underwater position) is mishandled and becomes fatal to the operation.

### Finding Description
The same dead-zone logic exists in `DebtToken.repay` (contracts/DebtToken.sol:442-450), where a holder of synths also cannot burn down a sub-floor residual — but `repayAll` covers the borrower's exit. [3](#0-2)  The critical asymmetry is in `liquidate`: there is no `liquidateAll` equivalent, and the `maxLiquidable` cap applies to the whole balance including the amount that would zero it out. An unprivileged attacker can manufacture the state without any privileged role:

- Deposit a small amount of collateral via `DepositToken.deposit` and issue synth via `DebtToken.issue` such that debt USD value is slightly above `debtFloorInUsd` but below `debtFloorInUsd / (1 - maxLiquidable)` (the issue-time floor check only requires `>= floor`, so the whole dead zone is open at issuance).
- The position becomes unhealthy through ordinary price movement or interest accrual (`DebtToken.accrueInterest` grows the balance into/past the boundary; an attacker can also donate msdTokens to push a victim's position, or simply wait — synth debt accrues interest while collateral value is fixed in USD terms).
- Once unhealthy, `pool.liquidate(synth, account, x, depositToken)` reverts for every `x`: `x <= D*maxLiquidable` leaves residual `< floor`; `x > D*maxLiquidable` (including `x == D`) hits `AmountGreaterThanMaxLiquidable`.

Because `depositToken_.seize` is the only path that force-moves a defaulter's collateral, and `unlockedBalanceOf` locks everything while `_issuableInUsd == 0` (DepositToken.sol:383-398), the position's collateral is frozen and the debt is permanently collectible by no one. [4](#0-3) 

### Impact Explanation
Permanent bad debt / protocol insolvency vector plus freezing of collateral:

- Underwater positions in the dead zone can never be liquidated, so the protocol must absorb them — each such position is guaranteed bad debt once collateral value < debt value.
- The liquidated-side collateral is permanently locked (`unlockedBalanceOf == 0` for the underwater account), so neither the borrower nor any liquidator can recover it — permanent freezing of funds, not merely temporary.
- An attacker can mass-produce these dust underwater positions across many accounts cheaply, converting a liquidation-liveness failure into systematic insolvency at scale.

### Likelihood Explanation
- Requires `debtFloorInUsd > 0` and `maxLiquidable < 1e18`. Both are live configuration values on deployed pools (the debt floor exists specifically to keep liquidations profitable, and `maxLiquidable` is a partial-liquidation cap; the test suite exercises `maxLiquidable = 0.5` and `debtFloor = $3,000` as representative settings).
- Reaching the dead zone needs only public entry points (`deposit`, `issue`) and normal market/interest drift — no governor, keeper, oracle corruption, or malicious LayerZero actor.
- Caveat: I could not confirm the exact on-chain `debtFloorInUsd`/`maxLiquidable` values from the indexed deployment artifacts; if a deployment sets `maxLiquidable == 1e18` or `debtFloorInUsd == 0`, the dead zone collapses for that pool.

### Recommendation
- In `Pool.liquidate`, exempt the boundary cases: allow `amountToRepay_ == _debtTokenBalance` even when it exceeds `maxLiquidable` (or at least when the position is deeply unhealthy / seize-capped by collateral balance), and treat residual debt `<= debtFloorInUsd` as liquidatable-to-zero rather than reverting.
- Symmetrically, cap `_totalSeized` by the account's actual deposit balance (closing the position entirely when collateral runs out) instead of reverting with `AmountIsTooHigh`, so bad debt is bounded by available collateral rather than becoming un-liquidatable.

### Proof of Concept
Hardhat/Foundry fork outline (requires a pool configured with `debtFloorInUsd = F > 0`, `maxLiquidable = m < 1`):

```solidity
// 1. Attacker deposits collateral and issues debt D such that
//    F <= V(D) < F / (1 - m).  // issue() only requires >= F
msdToken.deposit(collateralAmount, attacker);
debtToken.issue(amountForDebtJustAboveFloor, attacker);

// 2. Push position underwater (fork: move oracle price down slightly,
//    or warp so accrueInterest() inflates the debt).
vm.warp(block.timestamp + 365 days);
debtToken.accrueInterest();
(bool healthy,,,,) = pool.debtPositionOf(attacker);
assert(!healthy);

// 3. Every liquidation call reverts.
uint256 D = debtToken.balanceOf(attacker);
vm.expectRevert(AmountGreaterThanMaxLiquidable.selector);
pool.liquidate(synth, attacker, D, msdToken);                 // full repay blocked by m < 1

uint256 maxPartial = D * m / 1e18;
vm.expectRevert(RemainingDebtIsLowerThanTheFloor.selector);
pool.liquidate(synth, attacker, maxPartial, msdToken);        // residual < F

// Any smaller amount also leaves residual in (0, F) => same revert.
// Invariant broken: unhealthy positions are always liquidatable.
```

The existing unit tests already demonstrate both individual reverts (`test/Pool.test.ts` "debt floor" and "maxLiquidable" cases at lines ~408-451); the PoC only needs to combine them on one position whose debt USD value lies in `[F, F/(1-m))`. [5](#0-4)

### Citations

**File:** contracts/Pool.sol (L565-569)
```text
        uint256 _debtTokenBalance = _debtToken.balanceOf(account_);

        if (amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) {
            revert AmountGreaterThanMaxLiquidable();
        }
```

**File:** contracts/Pool.sol (L571-579)
```text
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

**File:** contracts/DepositToken.sol (L383-398)
```text
    function unlockedBalanceOf(address account_) public view override returns (uint256 _unlockedBalance) {
        IPool _pool = pool;

        (, , uint256 _debtInUsd, , uint256 _issuableInUsd) = _pool.debtPositionOf(account_);

        if (_debtInUsd == 0) {
            return balanceOf[account_];
        }

        if (_issuableInUsd > 0) {
            _unlockedBalance = Math.min(
                balanceOf[account_],
                _pool.masterOracle().quoteUsdToToken(address(underlying), _issuableInUsd.wadDiv(collateralFactor))
            );
        }
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
