### Title
Positions whose remaining debt falls below `debtFloorInUsd` can never be liquidated, allowing users to avoid liquidation and accrue bad debt - ([File: contracts/Pool.sol])

### Summary

`Pool.liquidate()` enforces two mutually exclusive constraints on `amountToRepay_`:

1. `amountToRepay_ / debtBalance <= maxLiquidable` — the liquidator cannot repay the full debt when `maxLiquidable < 100%`.
2. If `debtFloorInUsd > 0`, the post-liquidation debt must be either exactly `0` or `>= debtFloorInUsd` — otherwise it reverts with `RemainingDebtIsLowerThanTheFloor`.

When an account's total debt in a synthetic token, quoted in USD, drops below `debtFloorInUsd`, every permitted repayment amount reverts: any `amountToRepay_ < balance` leaves `0 < remaining < floor` (fails check 2), and `amountToRepay_ == balance` exceeds `maxLiquidable` (fails check 1). The position becomes permanently unliquidatable — the same deadlock class as the Notional report, where a protocol check makes liquidation impossible and lets sophisticated users avoid it.

### Finding Description

In `Pool.liquidate`:

```solidity
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
``` [1](#0-0) 

The floor is only enforced at mint time in `DebtToken._mint`, where `balance + amount` must exceed `debtFloorInUsd` in USD. There is no mechanism preventing an existing debt from later falling below the floor, because the floor is USD-denominated while the debt balance is token-denominated: [2](#0-1) 

A debt legally issued above the floor can fall below it when the synthetic token's oracle price decreases — the same price movement that simultaneously renders the position unhealthy when collateral value drops. `repay`/`repayAll` on `DebtToken` remain callable (they have no `maxLiquidable` cap), so the user is not trapped, but any third-party liquidation of that debt token reverts unconditionally.

This also applies more generally: for a debt `D` with `F <= D < F / (1 - maxLiquidable)`, the only repayments leaving `>= F` remaining are capped below `D - F`, and full repayment is capped by `maxLiquidable`, so the unliquidatable region is even wider than `D < F`. A sophisticated user can deliberately split debt across multiple synthetic tokens or time issuance so that a price move strands each debt in the dead zone, making their underwater position immune to liquidation while they retain the ability to self-repay via `repayAll`.

### Impact Explanation

An unhealthy position in the dead zone cannot be liquidated by anyone: `Pool.liquidate` always reverts for every `amountToRepay_`. If the position is undercollateralized (`debt > collateral`), the seized-collateral cap (`_totalSeized > balance` → `AmountIsTooHigh`) does not help either. The protocol is forced to accrue bad debt, and a sophisticated borrower can engineer this state to hold a permanently liquidation-proof position — matching the impact accepted as High in the reference report.

### Likelihood Explanation

- No privileged action is required by the victim or attacker; only `debtFloorInUsd > 0` and `maxLiquidable < 100%` configured by governance, both of which exist in the codebase and tests (`updateMaxLiquidable(0.5)`, `debtFloorInUsd` checks).
- The state is reached by ordinary oracle price movement of the synthetic or collateral assets. A user with multiple small debts in different synthetics has a materially higher chance of one entering the dead zone during volatility.
- Note: I was not able to verify the exact on-chain `debtFloorInUsd` and `maxLiquidable` deployment values from the indexed artifacts; the finding is conditional on `debtFloorInUsd > 0` being configured on a live pool (the code path is gated on it). If `debtFloorInUsd == 0` everywhere, the bug is dormant.

### Recommendation

Mirror the `DebtToken.repayAll` escape: in `Pool.liquidate`, skip the floor check when `amountToRepay_ == _debtTokenBalance` and allow full repayment of below-floor debt regardless of `maxLiquidable` (e.g., permit `amountToRepay_ == balance` when `_debtTokenBalance`'s USD value `< debtFloorInUsd`). Alternatively, allow liquidation amounts that bring remaining debt to exactly `0` or `>= floor` even when exceeding `maxLiquidable`.

### Proof of Concept

Hardhat test sketch (extends the existing `Pool.test.ts` fixture):

```ts
// given: alice has a healthy position with msEth debt just above debtFloorInUsd
await pool.updateMaxLiquidable(parseEther('0.5'))        // 50%
await pool.updateDebtFloorInUsd(floorUsd)               // e.g. $100
// alice issues debt D such that debtUsd(D) ~= 1.1 * floor

// when: oracle prices move — collateral drops (unhealthy) and msEth USD
// value of D drops below floor
await masterOracle.updatePrice(met.address, newLowPrice)
await masterOracle.updatePrice(msEth.address, lowerSynthPrice) // D_usd < floor

const { _isHealthy } = await pool.debtPositionOf(alice.address)
expect(_isHealthy).false

// then: every liquidation amount reverts
const debt = await msEthDebtToken.balanceOf(alice.address)

// full repayment -> exceeds maxLiquidable
await expect(
  pool.connect(bob).liquidate(msEth.address, alice.address, debt, msdMET.address)
).revertedWithCustomError(pool, 'AmountGreaterThanMaxLiquidable')

// max allowed partial repayment -> remaining debt below floor
await expect(
  pool.connect(bob).liquidate(msEth.address, alice.address, debt.div(2), msdMET.address)
).revertedWithCustomError(pool, 'RemainingDebtIsLowerThanTheFloor')

// meanwhile alice can still exit voluntarily — only liquidation is blocked
await msEthDebtToken.connect(alice).repayAll(alice.address) // succeeds
```

This demonstrates the invariant break: the liquidation path (solvency enforcement) is dead for a whole reachable class of states while the borrower retains full exit capability — the identical deadlock structure as the Notional H-11 report.

### Citations

**File:** contracts/Pool.sol (L565-579)
```text
        uint256 _debtTokenBalance = _debtToken.balanceOf(account_);

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

**File:** contracts/DebtToken.sol (L580-588)
```text
        uint256 _debtFloorInUsd = pool_.debtFloorInUsd();
        uint256 _balanceBefore = balanceOf(account_);

        if (
            _debtFloorInUsd > 0 &&
            masterOracle_.quoteTokenToUsd(address(syntheticToken), _balanceBefore + amount_) < _debtFloorInUsd
        ) {
            revert DebtLowerThanTheFloor();
        }
```
