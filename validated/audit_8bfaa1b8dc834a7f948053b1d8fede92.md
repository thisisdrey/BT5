### Title
Frontrunning `DebtToken.repay` with dust repayment causes victim's full-debt repayment to revert - ([File: contracts/DebtToken.sol](contracts/DebtToken.sol))

### Summary
`DebtToken.repay(onBehalfOf_, amount_)` burns `amount_` (net of fee) worth of debt from an arbitrary `onBehalfOf_` account using the caller's synthetic tokens. If the repaid amount exceeds the beneficiary's remaining debt, the function reverts via underflow in `balanceOf(onBehalfOf_) - _repaid` (and via `BurnAmountExceedsBalance` in `_burn`). Because anyone can repay on behalf of anyone else, an unprivileged griefer can front-run a victim's exact-amount `repay` transaction with a dust `repay(victim, dust)`, making the victim's transaction revert. This is the same bug class as the Aloe `Lender.repay` front-running issue.

### Finding Description
In `repay`:

```solidity
(_repaid, _fee) = quoteRepayOut(amount_);
...
uint256 _newDebtInUsd = _pool.masterOracle().quoteTokenToUsd(
    address(_syntheticToken),
    balanceOf(onBehalfOf_) - _repaid   // underflows if _repaid > balanceOf
);
...
_syntheticToken.burn(_msgSender, _repaid);
_burn(onBehalfOf_, _repaid);          // reverts BurnAmountExceedsBalance
``` [1](#0-0) 

There is no clamping or refund of excess. A user who computes the exact amount to clear their debt (e.g., via `quoteRepayIn(balanceOf(me))` and then calls `repay(me, amount)`) can be front-run by any EOA holding synthetic tokens calling `repay(victim, 1)`. After the dust repayment, `balanceOf(victim) < _repaid`, so line 446 underflows (or `_burn` reverts), and the victim's transaction fails. The attacker only sacrifices `1 wei` of synthetic token plus gas. The floor check does not help the victim; the attacker's dust repay succeeds because the remaining debt stays above `debtFloorInUsd` (or hits exactly a valid state), while the victim's subsequent over-amount repay reverts.

### Impact Explanation
Liveness / griefing: borrowers cannot reliably execute a full-repayment transaction in a single shot. Metronome positions back leveraged smart-farming and swap strategies where timely repayment or deleveraging matters; a forced revert forces the victim to resubmit with a recomputed amount or switch to `repayAll`, costing time and gas. This matches the accepted "temporary freezing of funds" / failed-repayment DoS impact of the analogous Aloe Medium finding. No funds are stolen, but repayment liveness is not guaranteed.

### Likelihood Explanation
Low-to-medium. The attacker needs to hold (or flash-obtain) a trivial amount of the synthetic token and monitor the mempool for exact-amount `repay` calls. The attack is cheap and repeatable, but its effect is limited to a failed transaction rather than a permanent lock — the victim can recover by calling `repayAll` or recomputing the amount, so the window of disruption is per-transaction.

### Recommendation
Cap the effective repayment at the beneficiary's outstanding debt and refund unused input to the payer, e.g.:

```solidity
uint256 _debt = balanceOf(onBehalfOf_);
uint256 _amount = amount_ > quoteRepayIn(_debt) ? ... : amount_;
// burn only min(_repaid, _debt) and leave/refund the excess synthetic to _msgSender
```

Alternatively, compute `_repaid = min(quoteRepayOut(amount_), balanceOf(onBehalfOf_))` before the floor check and `_burn`, so any excess `amount_` simply stays in the payer's balance instead of reverting.

### Proof of Concept
Foundry-style sketch (against a deployed pool where `debtToken`, `syntheticToken` exist):

```solidity
// setup: victim has debt D = debtToken.balanceOf(victim), and holds >= D synth
uint256 debt = debtToken.balanceOf(victim);
(uint256 amountIn,) = debtToken.quoteRepayIn(debt); // exact gross amount to clear debt
syntheticToken.transfer(attacker, 1);               // attacker holds dust synth

// attacker front-runs
vm.prank(attacker);
debtToken.repay(victim, 1);                         // burns 1 wei of victim's debt

// victim's exact-amount repay now reverts
vm.prank(victim);
vm.expectRevert(); // underflow at balanceOf - _repaid or BurnAmountExceedsBalance
debtToken.repay(victim, amountIn);
```

Confirmed reachable by an unprivileged caller: `repay` is `external`, only gated by `whenNotShutdown`, `nonReentrant`, and `onlyIfSyntheticTokenExists`, and the payer (`_msgSender()`) is decoupled from the beneficiary (`onBehalfOf_`).

### Citations

**File:** contracts/DebtToken.sol (L437-454)
```text
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
```
