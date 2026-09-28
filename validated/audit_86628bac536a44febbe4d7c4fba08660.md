### Title
Underwater positions leave permanently unrepayable debt since `liquidate` reverts when seized collateral exceeds the account's deposit balance, and no bad-debt mechanism exists - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
Metronome's `Pool.liquidate` has the same bad-debt gap as the referenced BendDAO finding. A liquidator can only repay synth debt up to the point where the seized collateral equals the account's `DepositToken` balance. When a collateral price crash (or accrued interest) makes a position's debt value exceed its collateral value, the residual debt can never be cleared through liquidation: `liquidate` reverts with `AmountIsTooHigh` whenever `_totalSeized > depositToken_.balanceOf(account_)`. The codebase contains no reserve, debt-socialization, or index-write-down mechanism, so the bad debt stays on `DebtToken` forever while the corresponding synthetic supply remains outstanding and undercollateralized.

### Finding Description
The flow in `Pool.liquidate`:

1. `quoteLiquidateMax` caps repayable debt at the lesser of `maxLiquidable * debtBalance` and the amount repayable by seizing the account's *entire* deposit balance (`quoteLiquidateIn(syntheticToken_, depositToken_.balanceOf(account_), ...)`) [1](#0-0) 
2. `liquidate` computes `_totalSeized` from `quoteLiquidateOut` (repay amount + liquidator incentive + protocol fee) and reverts if it exceeds the account's collateral balance [2](#0-1) 
3. After seizing all collateral, `_debtToken.burn(account_, amountToRepay_)` leaves `balanceOf(account_) > 0`; any subsequent `liquidate` call reverts at the same check because the account holds no collateral to seize [3](#0-2) 

This is exercised by the protocol's own tests: in `test/Pool.test.ts` under "when the position is unhealthy (collateral:debt < 1)", repaying the full debt reverts with `AmountIsTooHigh`, and after `quoteLiquidateMax` liquidation the account is left with ~zero `msdMET` balance while `msEthDebtToken.balanceOf(alice) > 0` — permanent unbacked debt [4](#0-3) .

The only escape is `DebtToken.repay(onBehalfOf_, amount_)` / `repayAll`, which let a third party burn their own synths to clear someone else's debt [5](#0-4) . Like BendDAO's disputed "treasury repays via `crossRepayERC20`" claim, this is a discretionary, out-of-band action by a benefactor — there is no reserve, auction, or socialized-loss mechanism in the codebase or docs, and no incentive for any party to absorb the loss.

Additionally, `Pool.swap` prices synth↔synth trades purely from oracle quotes with no solvency check, so undercollateralized synths continue trading at par, spreading the deficit across all synth holders [6](#0-5) .

### Impact Explanation
Protocol insolvency: once collateral value falls below debt value, a portion of outstanding synthetic supply is permanently backed by nothing. The residual `DebtToken` balance can never be burned via `liquidate` (always reverts `AmountIsTooHigh`), and accrues interest indefinitely. All synth holders collectively bear the shortfall — the synthetic asset's global backing ratio < 1 — with no on-chain mechanism to restore solvency. This matches the accepted "protocol insolvency" impact class.

### Likelihood Explanation
Requires an external condition (a sharp collateral price drop or prolonged interest accrual pushing debt above collateral), not attacker action — same trigger profile as the source finding. Modifiers (`whenNotShutdown`, `nonReentrant`, token-existence checks) and `maxLiquidable`/`debtFloorInUsd` do not prevent the state; `debtFloorInUsd` even worsens it by blocking dust-level repayments. `liquidate` remains callable; it simply cannot clear the leftover debt.

### Recommendation
Implement explicit bad-debt handling, e.g.:
- When `_totalSeized > depositToken_.balanceOf(account_)`, clamp the seize to the full balance and write off the residual debt (burn the `DebtToken` balance without requiring equivalent synth burn), socializing the loss; or
- Fund a protocol reserve/insurance fund from `feeCollector` proceeds that automatically repays bad debt via `repay`/`repayAll`; and document the mechanism in code and README.

### Proof of Concept
Reproducible in the existing Hardhat suite (fork not required; a Foundry equivalent is trivial):

```ts
// test/Pool.test.ts — extend the "position is unhealthy (collateral:debt < 1)" block
await masterOracle.updatePrice(met.address, toUSD('0.50')) // collateral crash
const amountToRepay = await pool.quoteLiquidateMax(msEth.address, alice.address, msdMET.address)
await pool.connect(liquidator).liquidate(msEth.address, alice.address, amountToRepay, msdMET.address)

// alice has ~0 collateral left but residual debt remains
expect(await msdMET.balanceOf(alice.address)).closeTo(0, 1600)
const leftover = await msEthDebtToken.balanceOf(alice.address)
expect(leftover).gt(0)

// bad debt can never be cleared: any further liquidation reverts
await expect(
  pool.connect(liquidator).liquidate(msEth.address, alice.address, leftover, msdMET.address)
).to.be.revertedWithCustomError(pool, 'AmountIsTooHigh') // Pool.sol:583
```

The existing tests at `test/Pool.test.ts:783-870` already assert both facts: repaying the full debt reverts with `AmountIsTooHigh`, and after max liquidation `msEthDebtToken.balanceOf(alice) > 0` with `msdMET.balanceOf(alice) ≈ 0`.

### Citations

**File:** contracts/Pool.sol (L429-439)
```text
        (uint256 _amountToRepay, , ) = quoteLiquidateIn(
            syntheticToken_,
            depositToken_.balanceOf(account_),
            depositToken_
        );

        _maxAmountToRepay = debtTokenOf[syntheticToken_].balanceOf(account_).wadMul(maxLiquidable);

        if (_amountToRepay < _maxAmountToRepay) {
            _maxAmountToRepay = _amountToRepay;
        }
```

**File:** contracts/Pool.sol (L512-524)
```text
    ) public view override returns (uint256 _amountOut, uint256 _fee) {
        _amountOut = _poolRegistry.masterOracle().quote(
            address(syntheticTokenIn_),
            address(syntheticTokenOut_),
            amountIn_
        );

        uint256 _swapFee = feeProvider.swapFees(address(syntheticTokenIn_), address(syntheticTokenOut_));

        if (_swapFee > 0) {
            _fee = _amountOut.wadMul(_swapFee);
            _amountOut -= _fee;
        }
```

**File:** contracts/Pool.sol (L581-585)
```text
        (_totalSeized, _toLiquidator, _fee) = quoteLiquidateOut(syntheticToken_, amountToRepay_, depositToken_);

        if (_totalSeized > depositToken_.balanceOf(account_)) {
            revert AmountIsTooHigh();
        }
```

**File:** contracts/Pool.sol (L587-593)
```text
        syntheticToken_.burn(_msgSender, amountToRepay_);
        _debtToken.burn(account_, amountToRepay_);
        depositToken_.seize(account_, _msgSender, _toLiquidator);

        if (_fee > 0) {
            depositToken_.seize(account_, _poolRegistry.feeCollector(), _fee);
        }
```

**File:** test/Pool.test.ts (L783-815)
```typescript
          it('should revert if paying more than needed to seize all deposit', async function () {
            const amountToRepay = await msEthDebtToken.balanceOf(alice.address)
            const tx = pool.connect(liquidator).liquidate(msEth.address, alice.address, amountToRepay, msdMET.address)

            // then
            await expect(tx).revertedWithCustomError(pool, 'AmountIsTooHigh')
          })

          it('should liquidate by repaying max possible amount (liquidateFee == 0)', async function () {
            // given
            await feeProvider.updateProtocolLiquidationFee(0)
            const depositBefore = await msdMET.balanceOf(alice.address)

            // when
            const amountToRepay = await pool.quoteLiquidateMax(msEth.address, alice.address, msdMET.address)

            const tx = await pool
              .connect(liquidator)
              .liquidate(msEth.address, alice.address, amountToRepay, msdMET.address)

            // then
            const [PositionLiquidated] = (await tx.wait()).events!.filter(({event}) => event === 'PositionLiquidated')
            const [, , , , depositSeized] = PositionLiquidated.args!

            const {_isHealthy} = await pool.debtPositionOf(alice.address)

            const remainder = 1600 // left over amount on user's deposit balance

            expect(_isHealthy).false
            expect(depositSeized).closeTo(depositBefore, remainder)
            expect(await msdMET.balanceOf(alice.address)).closeTo(BigNumber.from('0'), remainder)
            expect(await msEth.balanceOf(alice.address)).eq(userMintAmount)
            expect(await msEthDebtToken.balanceOf(alice.address)).gt(0)
```

**File:** contracts/DebtToken.sol (L418-457)
```text
    function repay(
        address onBehalfOf_,
        uint256 amount_
    )
        external
        override
        whenNotShutdown
        nonReentrant
        onlyIfSyntheticTokenExists
        returns (uint256 _repaid, uint256 _fee)
    {
        if (amount_ == 0) revert AmountIsZero();

        accrueInterest();

        address _msgSender = _msgSender();
        IPool _pool = pool;
        ISyntheticToken _syntheticToken = syntheticToken;

        (_repaid, _fee) = quoteRepayOut(amount_);
        if (_fee > 0) {
            _syntheticToken.seize(_msgSender, _pool.feeCollector(), _fee);
        }

        uint256 _debtFloorInUsd = _pool.debtFloorInUsd();
        if (_debtFloorInUsd > 0) {
            uint256 _newDebtInUsd = _pool.masterOracle().quoteTokenToUsd(
                address(_syntheticToken),
                balanceOf(onBehalfOf_) - _repaid
            );
            if (_newDebtInUsd > 0 && _newDebtInUsd < _debtFloorInUsd) {
                revert RemainingDebtIsLowerThanTheFloor();
            }
        }

        _syntheticToken.burn(_msgSender, _repaid);
        _burn(onBehalfOf_, _repaid);

        emit DebtRepaid(_msgSender, onBehalfOf_, amount_, _repaid, _fee);
    }
```
