### Title
Underwater positions with residual debt below `debtFloorInUsd` can never be liquidated, permanently trapping bad debt in the pool — (File: contracts/Pool.sol)

### Summary
The M-11 bug class is a permissionless "refresh/cleanup" function that reverts on a dust/zero boundary instead of skipping it, letting stale, unfavorable-to-the-protocol state persist. The Metronome analog is `Pool.liquidate`: it reverts with `RemainingDebtIsLowerThanTheFloor` whenever a liquidation would leave the account with `0 < newDebtInUsd < debtFloorInUsd`, and separately reverts with `AmountIsTooHigh` when the seized collateral would exceed the account's deposit balance. For an underwater (bad-debt) account whose entire collateral is worth less than `debt - debtFloorInUsd`, every allowed `amountToRepay_` hits one of these two reverts, so the position can never be liquidated at all.

### Finding Description
`liquidate` enforces the debt floor on the *post-liquidation* debt of the victim:

```solidity
if (debtFloorInUsd > 0) {
    uint256 _newDebtInUsd = masterOracle().quoteTokenToUsd(
        address(syntheticToken_),
        _debtTokenBalance - amountToRepay_
    );
    if (_newDebtInUsd > 0 && _newDebtInUsd < debtFloorInUsd) {
        revert RemainingDebtIsLowerThanTheFloor();
    }
}
``` [1](#0-0) 

Then it reverts if the collateral needed to cover the repayment exceeds the account's balance:

```solidity
if (_totalSeized > depositToken_.balanceOf(account_)) {
    revert AmountIsTooHigh();
}
``` [2](#0-1) 

`quoteLiquidateMax` caps `amountToRepay_` at what the remaining collateral can cover, which is strictly below full debt for an underwater account:

```solidity
(uint256 _amountToRepay, , ) = quoteLiquidateIn(
    syntheticToken_, depositToken_.balanceOf(account_), depositToken_);
_maxAmountToRepay = debtTokenOf[syntheticToken_].balanceOf(account_).wadMul(maxLiquidable);
if (_amountToRepay < _maxAmountToRepay) { _maxAmountToRepay = _amountToRepay; }
``` [3](#0-2) 

Attack path (unprivileged EOA only):
1. Attacker deposits collateral via `DepositToken.deposit` and issues max debt via `DebtToken.issue` such that debt value is just above `debtFloorInUsd` relative to collateral (or simply waits for the collateral price to drop / interest to accrue via `DebtToken.accrueInterest`, [4](#0-3) ).
2. Position becomes underwater: collateral value < debt value.
3. Any liquidator calling `liquidate` faces: repayments large enough to keep residual debt ≥ floor (or zero it) require seizing more collateral than exists → `AmountIsTooHigh`; repayments small enough to be collateral-backed leave residual debt < `debtFloorInUsd` → `RemainingDebtIsLowerThanTheFloor`. The floor check only passes when `_newDebtInUsd == 0`, i.e., full repayment, which is impossible for an underwater account.
4. The bad debt is permanently unliquidatable; the protocol must absorb it.

### Impact Explanation
Permanent freezing of the liquidation path for every underwater position whose residual debt would fall below `debtFloorInUsd` → protocol insolvency (bad debt that no liquidator can clear). The attacker loses their collateral but their synthetic tokens remain freely spendable/bridgeable, so the protocol's synthetic supply is left unbacked. This is worse than M-11's "excess rewards" — here the broken invariant is solvency itself.

### Likelihood Explanation
Requires `debtFloorInUsd > 0` on the deployed pool (it is a configured `PoolStorage` field) and an underwater account — both are normal operating conditions after any sharp collateral drawdown or sustained interest accrual. Any user can manufacture the state deliberately at low cost (the collateral is seized anyway in a successful liquidation). Likelihood is medium-to-high wherever a nonzero floor is set.

### Recommendation
Skip rather than revert when the residual debt is dust, mirroring the M-11 fix: in `liquidate`, when `amountToRepay_` is capped by the account's collateral (i.e., equals `quoteLiquidateMax`), allow the residual debt to fall below `debtFloorInUsd` — e.g., waive the floor check when the liquidation seizes the account's entire collateral balance, or auto-clear the remaining dust debt to zero (socialize/write-off) instead of reverting. Alternatively, exempt the floor check when `_debtTokenBalance - amountToRepay_` is below the floor *and* the liquidator is repaying the maximum collateral-backed amount.

### Proof of Concept
```solidity
// Foundry fork test sketch (mainnet pool where debtFloorInUsd > 0)
function testUnliquidatableBadDebt() public {
    // alice: deposit collateral, issue max debt
    msdMET.deposit(collateralAmount, alice);
    (, , , , uint256 issuable) = pool.debtPositionOf(alice);
    uint256 issueAmt = masterOracle.quoteUsdToToken(address(msUSD), issuable);
    msUSDDebt.issue(issueAmt, alice);

    // push position underwater via interest accrual
    vm.warp(block.timestamp + 365 days);
    msUSDDebt.accrueInterest();
    (bool healthy,,,,) = pool.debtPositionOf(alice);
    assertFalse(healthy);
    assertGt(pool.debtFloorInUsd(), 0);

    // liquidator tries the max collateral-backed repay:
    uint256 maxRepay = pool.quoteLiquidateMax(msUSD, alice, msdMET);
    vm.prank(liquidator);
    vm.expectRevert(Pool.RemainingDebtIsLowerThanTheFloor.selector);
    pool.liquidate(msUSD, alice, maxRepay, msdMET);

    // liquidator tries full repayment:
    uint256 fullDebt = msUSDDebt.balanceOf(alice);
    vm.prank(liquidator);
    vm.expectRevert(Pool.AmountIsTooHigh.selector); // seize > collateral balance
    pool.liquidate(msUSD, alice, fullDebt, msdMET);

    // no amountToRepay_ in between succeeds -> bad debt is permanent
}
```
Caveat to verify on a fork: the exact `debtFloorInUsd` value configured on each deployed pool determines the size of the unliquidatable band; where the floor is `0` this specific instance does not trigger.

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

**File:** contracts/Pool.sol (L583-585)
```text
        if (_totalSeized > depositToken_.balanceOf(account_)) {
            revert AmountIsTooHigh();
        }
```

**File:** contracts/DebtToken.sol (L156-180)
```text
    function accrueInterest() public override {
        (
            uint256 _interestAmountAccrued,
            uint256 _debtIndex,
            uint256 _lastTimestampAccrued
        ) = _calculateInterestAccrual();

        if (block.timestamp == _lastTimestampAccrued) {
            return;
        }

        lastTimestampAccrued = block.timestamp;

        if (_interestAmountAccrued > 0) {
            totalSupply_ += _interestAmountAccrued;
            debtIndex = _debtIndex;

            // Note: Address states where minting will fail (e.g. the token is inactive, it reached max supply, etc)
            try syntheticToken.mint(pool.feeCollector(), _interestAmountAccrued + pendingInterestFee) {
                pendingInterestFee = 0;
            } catch {
                pendingInterestFee += _interestAmountAccrued;
            }
        }
    }
```
