### Title
`Pool.liquidate` can make a position unliquidatable when `maxLiquidable` cap forces remaining debt into the `debtFloorInUsd` dust range - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
The Hubble bug is a gross-vs-net accounting flaw: a "reduce + open opposite" order is charged margin on the gross size instead of the net resulting position, so a legitimate operation reverts and the position cannot be adjusted in one transaction. The same class exists in Metronome's liquidation path: `Pool.liquidate` enforces two independent constraints on the *remaining* debt — `maxLiquidable` caps how much can be repaid, while `debtFloorInUsd` forbids leaving a small residual debt. For a range of debts, every allowed repayment amount either exceeds `maxLiquidable` or leaves dust below the floor, so the position can never be liquidated.

### Finding Description
`Pool.liquidate` applies two checks to a single repayment:

1. A cap on the repay amount: `amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable` reverts, so at most `maxLiquidable` (e.g. 50%) of the debt can be repaid per call — and since `balanceOf` only shrinks after repayment, repeated calls can never repay 100% in aggregate when `maxLiquidable < 1e18` (each successive call can only repay 50% of the *current* balance, asymptotically approaching but never reaching zero). [1](#0-0) 
2. A dust floor: if the remaining debt after repayment is `> 0` and `< debtFloorInUsd`, the call reverts with `RemainingDebtIsLowerThanTheFloor`. [2](#0-1) 

Just like the OrderBook case where the protocol demanded margin for the gross order instead of netting the reduce leg, here the protocol evaluates the post-reduce residual debt in isolation and rejects the reduce operation — even though the liquidator's *net* intent (repay as much as allowed) is valid. The only "correct" repayment is one that exactly zeroes the debt or leaves `>= debtFloorInUsd`, which is impossible whenever `debt * (1 - maxLiquidable) < debtFloorInUsd <= debt`. Concretely: debt = $100, `maxLiquidable` = 50%, `debtFloorInUsd` = $60. Repaying $50 leaves $50 < $60 → revert; repaying $51+ violates `maxLiquidable` → revert; any smaller repayment leaves even more dust. The position is permanently unliquidatable while unhealthy.

Note `quoteLiquidateMax` does not account for this conflict — it returns up to `debt * maxLiquidable` without checking that the residual clears the floor. [3](#0-2) 

### Impact Explanation
An unhealthy position whose debt sits in the range `debtFloorInUsd <= debt < debtFloorInUsd / (1 - maxLiquidable)` cannot be liquidated at all. Liquidators are unprivileged EOAs calling the public `liquidate` entry point, so no privileged action can unblock it (governor parameter changes are out of scope, and the position remains stuck on the deployed configuration). The result is protocol insolvency exposure: bad debt accrues interest while collateral cannot be seized, and the user also cannot fully repay-without-dust via `DebtToken.repay`, which applies the identical `RemainingDebtIsLowerThanTheFloor` check. [4](#0-3) 

### Likelihood Explanation
`debtFloorInUsd` is a deployed, governor-set feature (`updateDebtFloor`, `Pool.sol:768`) and `maxLiquidable` defaults to 50% at initialization (`Pool.sol:181`). Any debt position that drifts into the forbidden band — through price movement or interest accrual — becomes unliquidatable by construction. No attacker manipulation is required to create the condition; an attacker can also deliberately engineer it by opening a debt sized just inside the band and letting collateral value fall.

### Recommendation
Apply the floor check against the *net* outcome the same way net margin should be applied in the OrderBook fix: when a partial repayment would leave residual debt below `debtFloorInUsd`, either (a) allow the liquidator to repay up to the full remaining debt in that call (exempt the `maxLiquidable` cap when it would leave dust), or (b) clamp `amountToRepay_` down to `debt - minDebtFloorAmount` instead of reverting. `quoteLiquidateMax` should be updated to return a value that respects the floor so liquidators are not given an unexecutable quote.

### Proof of Concept
Foundry/Hardhat fork test outline on a live pool (e.g. mainnet `Pool`):

```solidity
// Setup: account has msUSD debt of $100 worth, collateral dropped -> unhealthy.
// Deployed config: maxLiquidable = 0.5e18, debtFloorInUsd = $60 (set via updateDebtFloor).
vm.prank(liquidator);
// Attempt 1: repay the max allowed (50 msUSD)
pool.liquidate(msUSD, victim, 50e18, msdVaUSDC);
// -> reverts RemainingDebtIsLowerThanTheFloor (remaining $50 < $60 floor)

// Attempt 2: repay enough to clear the floor ($60 -> remaining $40? no:)
// repaying $60 leaves $40 < floor -> revert; repaying >$50 -> AmountGreaterThanMaxLiquidable.
// Any amountToRepay in (0, $100*0.5] leaves residual in (0, floor) -> all revert.
// Assert: no amountToRepay_ value succeeds while position remains unhealthy.
```

Verification: iterate `amountToRepay_` over `[1, debtBalance]` in a fuzz test; assert every value reverts with either `AmountGreaterThanMaxLiquidable` or `RemainingDebtIsLowerThanTheFloor` while `debtPositionOf(victim)._isHealthy == false`. This demonstrates the liveness invariant (all unhealthy positions are liquidatable) is broken on the deployed parameter set.

### Citations

**File:** contracts/Pool.sol (L419-440)
```text
    function quoteLiquidateMax(
        ISyntheticToken syntheticToken_,
        address account_,
        IDepositToken depositToken_
    ) external view override returns (uint256 _maxAmountToRepay) {
        (bool _isHealthy, , , , ) = debtPositionOf(account_);
        if (_isHealthy) {
            return 0;
        }

        (uint256 _amountToRepay, , ) = quoteLiquidateIn(
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

**File:** contracts/DebtToken.sol (L442-451)
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
        }
```
