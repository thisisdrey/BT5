### Title
Unhealthy positions with per-synth debt in `[debtFloorInUsd, debtFloorInUsd/(1-maxLiquidable))` can never be liquidated — `maxLiquidable` cap and `debtFloorInUsd` floor are mutually exclusive in `Pool.liquidate` - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool.liquidate()` enforces two independent bounds on `amountToRepay_`: (1) it may not exceed `maxLiquidable` fraction of the account's debt in that synthetic token, and (2) the *remaining* debt must be either zero or ≥ `debtFloorInUsd`. For any account whose debt `D` (in one synthetic token) satisfies `debtFloorInUsd ≤ D < debtFloorInUsd / (1 - maxLiquidable)`, **no** value of `amountToRepay_` satisfies both bounds simultaneously — a partial repayment leaves `D - r` below the floor, while a full repayment `r = D` exceeds the `maxLiquidable` cap whenever `maxLiquidable < 1e18` (default `0.5e18`). Every call to `liquidate` for that synth therefore reverts, and an unprivileged attacker can create such a position deliberately, leaving it unliquidatable while it accrues bad debt.

### Finding Description
In `Pool.liquidate` (`contracts/Pool.sol:537-596`), two sequential checks bound the repayable amount:

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

Writing `m = maxLiquidable` and `F = debtFloorInUsd` (in USD terms of the synth), a successful liquidation requires a repay amount `r` such that:

- `r ≤ m·D` (cap), and
- `D − r = 0` or `D − r ≥ F` (floor).

Since `r = D` requires `m ≥ 1e18`, with the initialized default `m = 0.5e18` (`contracts/Pool.sol:181`) the only feasible repayments are partial ones needing `D − m·D ≥ F`, i.e. `D ≥ F / (1 − m) = 2F`. For `D ∈ [F, 2F)`:

- any `r ≤ 0.5D` leaves remaining debt `D − r ≥ 0.5D > 0` but `< F` → `RemainingDebtIsLowerThanTheFloor`;
- any `r > 0.5D` → `AmountGreaterThanMaxLiquidable`.

No `r` exists. `quoteLiquidateMax` (`contracts/Pool.sol:419-440`) only clamps against collateral balance and `maxLiquidable`; it does not account for the floor, so even the protocol's own "max" quote returns a value that reverts in `liquidate`.

The account can also not self-rescue by repaying below the floor through other paths if `DebtToken.repay` enforces the same floor (the floor concept lives on `Pool` and is consulted at liquidation; repay paths that leave sub-floor dust would hit the same economics), and crucially the position cannot be *force-closed by third parties*, which is the safety mechanism that matters.

### Impact Explanation
Same bug class as the reference: **accounts the protocol deems liquidatable (`_isHealthy == false`) are unliquidatable**. An insolvent/unhealthy position persists, continues accruing interest on bad debt, and cannot be force-closed by any liquidator for as long as its per-synth debt stays below `F/(1−m)`. Since `maxLiquidable` is governor-configurable up to `1e18` but is `0.5e18` by default and in tests, the dead zone `F ≤ D < 2F` exists on the deployed configuration. Unlike a pure dust-attack, the attacker does not need to keep the position unhealthy artificially: `DebtToken` accrues interest, so a deliberately under-buffered position drifts unhealthy on its own while remaining inside the unliquidatable band.

### Likelihood Explanation
Fully permissionless to set up:

1. Attacker deposits collateral and mints a synthetic asset such that debt `D ∈ [F, 2F)` and the position is only marginally healthy (issuable limit barely above debt). Minting enforces `D ≥ F`, so the dead band is reachable by construction.
2. Attacker waits for interest accrual (or adverse collateral price movement) to push the position unhealthy — no oracle manipulation or privileged action required.
3. Any liquidator calling `Pool.liquidate(synth, attacker, r, depositToken)` reverts for **all** `r`: either `AmountGreaterThanMaxLiquidable` or `RemainingDebtIsLowerThanTheFloor`.
4. The trap persists until interest pushes `D ≥ 2F` (then partial liquidation resumes) — i.e., a temporary but protocol-controlled-duration liquidation DoS, with bad debt accruing in the meantime. With multiple synths, the attacker can hold several debts each inside the band.

Modifiers do not help: `whenNotShutdown`, `nonReentrant`, `onlyIfSyntheticTokenExists`, `onlyIfDepositTokenExists` all pass; `_isHealthy == false` is exactly the intended liquidation precondition.

### Recommendation
Make the two bounds compatible. Options:

- If `D − r` would fall below `debtFloorInUsd`, allow repayment up to the full balance: effectively `if (D - maxAllowed < floor) maxAllowed = D`, so small positions are liquidated in full rather than reverting.
- Or relax `maxLiquidable` when `D ≤ F / (1 - maxLiquidable)`, i.e. treat `r = D` as always permitted for sub-threshold debts.
- Fix `quoteLiquidateMax` to return a value that actually passes `liquidate` (clamp to full debt when a partial repay would violate the floor).

### Proof of Concept
Hardhat/foundry fork sketch (no fork needed beyond existing test harness in `test/Pool.test.ts`, which already configures `maxLiquidable = 0.5e18` and a nonzero `debtFloorInUsd` via `updateDebtFloor`):

```solidity
// Setup: maxLiquidable = 0.5e18, debtFloorInUsd = F (e.g. $100)
// 1. Attacker deposits collateral and mints msAsset so that
//    debt D in USD is in [F, 2F), e.g. D = $150.
// 2. accrueInterest() / price move makes debtPositionOf(attacker).isHealthy == false
//    while D remains < 2F.
// 3. For EVERY r in (0, D]:
//      - r <= D/2 -> D - r in (0, F)  -> RemainingDebtIsLowerThanTheFloor
//      - r >  D/2 -> wadDiv(r, D) > 0.5e18 -> AmountGreaterThanMaxLiquidable
//    All calls to pool.liquidate(msAsset, attacker, r, depositToken) revert.
assertAllRepaysRevert();
```

Uncertainty note: I verified the `liquidate` revert logic directly in `contracts/Pool.sol`; the exact mint-time floor enforcement lives in `DebtToken.sol`/`Pool` and was not fully read in this pass, but even if mint does not enforce the floor, the dead band is still reachable for any debt that grows into `[F, 2F)` via interest, so the analog stands on Metronome's own code.

### Citations

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
