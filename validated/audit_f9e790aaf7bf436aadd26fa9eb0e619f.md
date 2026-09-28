### Title
Partial liquidation can restore an unhealthy position to healthy, delaying further liquidations and risking bad debt - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool.liquidate` reverts with `PositionIsHealthy` whenever `debtPositionOf(account_)` reports the account as healthy. Because a partial liquidation repays debt while seizing proportionally more collateral (liquidator incentive + protocol fee), a sufficiently sized partial liquidation can push an underwater position back above its issuable limit, making it "healthy" again. Any subsequent liquidation then reverts until the collateral price falls further. This is the same bug class as the referenced CDPVault finding: there is no sticky "unhealthy" flag, so a small, well-priced partial liquidation buys the position time it should not have. [1](#0-0) 

### Finding Description
In `contracts/Pool.sol`, `liquidate` performs the health gate on the spot state only:

```solidity
(bool _isHealthy, , , , ) = debtPositionOf(account_);
if (_isHealthy) {
    revert PositionIsHealthy();
}
if (amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) {
    revert AmountGreaterThanMaxLiquidable();
}
``` [2](#0-1) 

`quoteLiquidateOut` prices the seized collateral as `repayValue * (1 + liquidatorIncentive) + fee`, so each repayment removes more USD of collateral than the USD of debt burned — but the debt reduction per unit of remaining collateral still improves the collateralization ratio. The repo's own test suite demonstrates the effect directly: after a partial liquidation, `debtPositionOf` returns `_isHealthy == true` (`test/Pool.test.ts:601`). [3](#0-2) [4](#0-3) 

Once healthy, every further `liquidate` call reverts on the `PositionIsHealthy` check until the oracle price drops enough to make the position unhealthy again. `maxLiquidable` only caps the *size* of a single liquidation — it does not require that a liquidation actually reduce risk adequately, so an attacker can deliberately choose `amountToRepay_` that lands the position just above the health threshold.

`CanNotLiquidateOwnPosition` prevents the position owner from calling `liquidate` on themselves, but does not stop the attack: the position owner can hand synth tokens to a second EOA/contract they control (or an accomplice) which calls `liquidate` on their behalf. The "accomplice" burns synth and receives collateral at the liquidation discount — economically equivalent to the self-liquidation described in the original report, since the seized collateral incentive means the accomplice profits or the owner can reimburse them out-of-band. The attacker pays only the liquidation discount cost rather than adding new collateral, which is strictly cheaper than the intended remediation path (deposits or `repay`). [5](#0-4) 

### Impact Explanation
Liquidation liveness is weakened exactly when it matters most. An unhealthy position can be repeatedly nudged back to "healthy" by minimal-cost partial liquidations, blocking all liquidation attempts while the collateral price continues to decline. When the price finally drops enough to re-flag the position, it is deeper underwater; another partial liquidation can again restore health. Iterating this pattern during a sustained price decline leaves the protocol holding positions that eventually become undercollateralized below 100% — bad debt socialized across the pool — where the per-step cost to the attacker is small relative to the insolvency risk transferred to the protocol. This maps to the "protocol insolvency / delayed liquidation" invariant break.

### Likelihood Explanation
- Entry point is fully public (`Pool.liquidate`, no role gating; `whenNotShutdown`/`nonReentrant` do not restrict callers). [6](#0-5) 
- The only requirement is holding enough of the synthetic token to repay, obtainable via `DebtToken.issue` (minting against own collateral) or the open market/flash-issue paths.
- It requires a falling collateral price and a position near the liquidation boundary — routine market conditions, not an exotic setup.
- Attack cost is low: the accomplice actually *earns* the `liquidatorIncentive` on the seized collateral, so the griefing can be net-profitable for the colluding pair.

### Recommendation
Make unhealthy status sticky: flag an account as liquidatable when it first becomes unhealthy (or record a timestamped unhealthy marker) and only clear it when the position is restored by the owner through deposits/repayments — not by third-party liquidations. Alternatives: require liquidations on freshly-flagged positions to bring collateralization to a safety buffer above the liquidation threshold (e.g., enforce a post-liquidation minimum health improvement or force liquidation of the full `maxLiquidable` amount when a partial liquidation would leave the position healthy), or allow liquidations to proceed while the position remains within a defined margin of the threshold.

### Proof of Concept
Hardhat fork test sketch consistent with `test/Pool.test.ts` fixtures (`pool`, `msEth`, `msdMET`, `masterOracle`, `feeProvider`):

```typescript
it('partial liquidation restores health and blocks further liquidations', async function () {
  // 1. Alice deposits MET and mints msETH near max capacity.
  // 2. Drop MET price so Alice is unhealthy.
  await masterOracle.updatePrice(met.address, toUSD('0.70'));
  const { _isHealthy } = await pool.debtPositionOf(alice.address);
  expect(_isHealthy).false;

  // 3. Accomplice EOA (funded by Alice with msETH) partially liquidates,
  //    choosing amountToRepay so the position just becomes healthy.
  const debt = await msEthDebtToken.balanceOf(alice.address);
  const minRepayUsd = await getMinLiquidationAmountInUsd(pool, alice.address, msdMET);
  const amountToRepay = await masterOracle.quoteUsdToToken(msEth.address, minRepayUsd);
  await msEth.connect(alice).transfer(accomplice.address, amountToRepay);
  await pool.connect(accomplice).liquidate(msEth.address, alice.address, amountToRepay, msdMET.address);

  // 4. Position is healthy again (same behavior asserted in existing tests).
  const { _isHealthy: healthyAfter } = await pool.debtPositionOf(alice.address);
  expect(healthyAfter).true;

  // 5. Price keeps falling but position still shows healthy at current price;
  //    all liquidation attempts revert until price drops below the new threshold.
  await masterOracle.updatePrice(met.address, toUSD('0.68'));
  await expect(
    pool.connect(liquidator).liquidate(msEth.address, alice.address, debt.div(4), msdMET.address)
  ).revertedWithCustomError(pool, 'PositionIsHealthy');
});
```

The revert at step 5 demonstrates that a still-falling (or already low) collateral price cannot trigger liquidation while the position is pinned just above the health threshold, confirming the delayed-liquidation invariant break on `contracts/Pool.sol:561-563`.

### Citations

**File:** contracts/Pool.sol (L455-471)
```text
    ) public view override returns (uint256 _totalToSeize, uint256 _toLiquidator, uint256 _fee) {
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

**File:** contracts/Pool.sol (L537-548)
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
```

**File:** contracts/Pool.sol (L551-555)
```text
        address _msgSender = _msgSender();

        if (amountToRepay_ == 0) revert AmountIsZero();
        if (_msgSender == account_) revert CanNotLiquidateOwnPosition();

```

**File:** contracts/Pool.sol (L559-569)
```text
        (bool _isHealthy, , , , ) = debtPositionOf(account_);

        if (_isHealthy) {
            revert PositionIsHealthy();
        }

        uint256 _debtTokenBalance = _debtToken.balanceOf(account_);

        if (amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) {
            revert AmountGreaterThanMaxLiquidable();
        }
```

**File:** test/Pool.test.ts (L595-604)
```typescript
            const {_isHealthy: isHealthyAfter, _depositInUsd: collateralInUsdAfter} = await pool.debtPositionOf(
              alice.address
            )
            const collateralAfter = await masterOracle.quoteUsdToToken(met.address, collateralInUsdAfter)
            const lockedCollateralAfter = await msdMET.lockedBalanceOf(alice.address)

            expect(isHealthyAfter).true
            expect(collateralAfter).closeTo(collateralBefore.sub(depositSeized), 1)
            expect(lockedCollateralAfter).gt(0)
            expect(await msdMET.balanceOf(alice.address)).eq(collateralBefore.sub(depositSeized))
```
