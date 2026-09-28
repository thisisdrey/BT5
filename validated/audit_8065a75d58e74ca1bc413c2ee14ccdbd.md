### Title
Unliquidatable positions via `debtFloorInUsd`/`maxLiquidable` interaction allow permanent freezing of collateral and bad-debt DoS - ([File: contracts/Pool.sol])

### Summary
`Pool.liquidate` enforces two conflicting bounds: a repayment cannot exceed `maxLiquidable` (default `0.5e18`, i.e. 50%) of the account's debt, and the *remaining* debt after repayment cannot be a non-zero value below `debtFloorInUsd`. For any debt `D` with `debtFloorInUsd < D < 2 * debtFloorInUsd`, every legal repayment either exceeds `maxLiquidable` (when repaying all) or leaves a remainder `< debtFloorInUsd` (when repaying ≤50%). Such a position is therefore impossible to liquidate, ever — an unprivileged analog of the crafted-query optimizer DoS in CVE-2024-21230.

### Finding Description
`Pool.liquidate` at `contracts/Pool.sol:567-579` applies both checks sequentially:

```solidity
if (amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) {
    revert AmountGreaterThanMaxLiquidable();
}
...
uint256 _newDebtInUsd = masterOracle().quoteTokenToUsd(
    address(syntheticToken_),
    _debtTokenBalance - amountToRepay_
);
if (_newDebtInUsd > 0 && _newDebtInUsd < debtFloorInUsd) {
    revert RemainingDebtIsLowerThanTheFloor();
}
```

`maxLiquidable` is initialized to `0.5e18` (`contracts/Pool.sol:181`), so the maximum repayable amount is `D/2`. If `D/2 < debtFloorInUsd`, the remainder `D - D/2 = D/2` is both non-zero and below the floor — the call reverts. Any smaller repayment also reverts; repaying `D` (remainder = 0, allowed by the floor check) reverts on `AmountGreaterThanMaxLiquidable`. No admissible `amountToRepay_` exists.

`DebtToken._mint` (`contracts/DebtToken.sol:583-588`) only requires `D >= debtFloorInUsd`, so the range `[debtFloorInUsd, 2*debtFloorInUsd)` is freely mintable by any EOA. `DebtToken.repay` (`contracts/DebtToken.sol:443-451`) has the same floor check, so the victim position cannot even be partially repaired by anyone below the floor — only `repayAll` clears it, and the borrower is not required to do so.

Because the position can never be liquidated, the debt accrues interest unboundedly while `DepositToken.unlockedBalanceOf` (`contracts/DepositToken.sol:383-398`) returns 0 once the position is unhealthy, permanently locking the deposited collateral. If the position is undercollateralized, the protocol is forced to carry bad debt it can never clear.

### Impact Explanation
- Permanent freezing of user funds: all collateral backing a floor-bound unhealthy position is locked in `DepositToken`/`Treasury` forever (withdraw/transfer revert via `_revertIfLocked`).
- Protocol insolvency vector: an attacker can deliberately open a `debtFloorInUsd < D < 2*debtFloorInUsd` position, let it (or push it via a same-transaction oracle/AMM move) go underwater, and the protocol must absorb the shortfall with no liquidation path — liquidators are hard-reverted regardless of `amountToRepay_`.
- This mirrors the CVE class: a low-privileged, network-reachable action that reliably triggers a liveness failure (here, permanently bricked liquidation pathway) rather than memory corruption.

### Likelihood Explanation
- Fully permissionless: `DebtToken.issue` and `DepositToken.deposit` are public; no privileged role needed to create the poisoned position.
- Trigger requires the position to be liquidatable (`debtPositionOf._isHealthy == false`), which occurs naturally via price drift or via allowed same-transaction price manipulation.
- Interest accrual continuously grows `D`, so a position can also *become* floor-bound as debt grows into the band, making organic (non-adversarial) positions unliquidatable too.
- Only mitigation is governance raising `maxLiquidable` to 1e18 or lowering `debtFloorInUsd`; neither is guaranteed in the deployed configuration (default `maxLiquidable = 0.5e18`).

### Recommendation
In `Pool.liquidate`, exempt full repayments from the `maxLiquidable` bound, or exempt liquidations from the floor check when they leave residual debt, e.g.:

```solidity
bool _repaysAll = amountToRepay_ == _debtTokenBalance;
if (!_repaysAll && amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) {
    revert AmountGreaterThanMaxLiquidable();
}
```

Alternatively, when `D <= 2 * debtFloorInUsd`, allow the liquidator to clear the entire debt so no position can sit in the unliquidatable band.

### Proof of Concept
Hardhat sketch (based on `test/Pool.test.ts` fixtures):

```ts
// setup: pool.updateDebtFloor(parseEther('3000')); // $3,000
// alice deposits MET collateral and issues msETH worth $4,000
// -> debt D = $4,000, debtFloor = $3,000, D/2 = $2,000 < floor

// price drops: masterOracle.updatePrice(met.address, toUSD('0.9'))
// -> position is unhealthy

// liquidator tries maxLiquidable (50%):
await expect(
  pool.connect(liq).liquidate(msEth.address, alice.address, debt.div(2), msdMET.address)
).revertedWithCustomError(pool, 'RemainingDebtIsLowerThanTheFloor');

// liquidator tries full repay:
await expect(
  pool.connect(liq).liquidate(msEth.address, alice.address, debt, msdMET.address)
).revertedWithCustomError(pool, 'AmountGreaterThanMaxLiquidable');

// any amount in between reverts on one of the two checks;
// alice's collateral stays locked: msdMET.unlockedBalanceOf(alice) === 0
```

The two existing tests at `test/Pool.test.ts:408-436` already demonstrate each revert independently; combining them on the same position shows the gap. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** contracts/Pool.sol (L181-182)
```text
        maxLiquidable = 0.5e18; // 50%
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

**File:** contracts/DebtToken.sol (L443-451)
```text
        if (_debtFloorInUsd > 0) {
            uint256 _newDebtInUsd = _pool.masterOracle().quoteTokenToUsd(
                address(_syntheticToken),
                balanceOf(onBehalfOf_) - _repaid
            );
            if (_newDebtInUsd > 0 && _newDebtInUsd < _debtFloorInUsd) {
                revert RemainingDebtIsLowerThanTheFloor();
            }
        }
```

**File:** contracts/DebtToken.sol (L583-588)
```text
        if (
            _debtFloorInUsd > 0 &&
            masterOracle_.quoteTokenToUsd(address(syntheticToken), _balanceBefore + amount_) < _debtFloorInUsd
        ) {
            revert DebtLowerThanTheFloor();
        }
```

**File:** contracts/DepositToken.sol (L383-397)
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
```
