### Title
Debt floor + `maxLiquidable` interaction permanently bricks `liquidate` for small positions, leaving bad debt unrecoverable - ([File: contracts/Pool.sol])

### Summary
`Pool.liquidate` enforces two independent bounds: `amountToRepay_` may not exceed `maxLiquidable` fraction of the debt, and the post-liquidation remaining debt must be either zero or above `debtFloorInUsd`. When `maxLiquidable < 1e18` and a position's debt value sits below `debtFloorInUsd`, every possible `amountToRepay_` reverts — full repayment fails `AmountGreaterThanMaxLiquidable`, and any partial repayment leaves a remainder under the floor and fails `RemainingDebtIsLowerThanTheFloor`. The position becomes permanently unliquidatable, analogous to the Privoxy assertion-failure DoS: a crafted state makes the request handler always crash.

### Finding Description
In `Pool.liquidate` (`contracts/Pool.sol:537-596`):

- `amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable` reverts, so the liquidator can burn at most `maxLiquidable` (e.g., 50%) of the debt per call (lines 567-569).
- If `debtFloorInUsd > 0`, the remaining debt `_debtTokenBalance - amountToRepay_` (quoted to USD) must be `0` or `>= debtFloorInUsd`, otherwise `RemainingDebtIsLowerThanTheFloor` (lines 571-579).

Since a full repayment is the only way to reach `0` remaining debt, and a full repayment is blocked whenever `maxLiquidable < 1e18`, a debt whose USD value is below the floor admits no valid `amountToRepay_`. The floor check is enforced only in `liquidate`; the mint-side floor check in `DebtToken.issue` prevents *opening* a sub-floor debt but does not prevent a debt from *becoming* sub-floor afterward (partial repayments, interest accrual changing USD value, or oracle repricing of the synthetic). `quoteLiquidateMax` (`contracts/Pool.sol:419-440`) offers no escape — it just returns the capped amount that then reverts.

`seize` on `DepositToken` is only reachable via `liquidate` (`onlyIfCanSeize`), so the collateral backing the bad debt is frozen in the protocol while the debt is unrecoverable.

### Impact Explanation
Permanent denial of the liquidation function for the affected position. If the position is underwater (collateral < debt, which naturally happens as prices move), liquidators cannot touch it; the pool is forced to carry the bad debt, leading to protocol insolvency on that position and loss for synth holders/LPs. An attacker can deliberately engineer this: open a debt just above the floor, partially repay to land the remainder just below `debtFloorInUsd`, and the position becomes immune to liquidation forever at near-zero cost.

### Likelihood Explanation
Requires `debtFloorInUsd > 0` (the feature exists in deployed Pool configs across mainnet/base/optimism/swell/hemi/bsc) and `maxLiquidable < 1e18` (e.g., 50% as exercised in `test/Pool.test.ts:439-451`). Both are normal deployed parameters. No privileged role is needed — the attacker uses only public `DebtToken.issue`/`repay` and `Pool.liquidate`. The window of opportunity is small-dollar (debt must sit under the floor), so per-position damage is capped by `debtFloorInUsd`, but it is fully permissionless and repeatable across accounts, and the freeze is permanent (only a governance change to the floor or `maxLiquidable` unsticks it).

### Recommendation
When `debtFloorInUsd > 0`, allow liquidation calls that repay the entire remaining debt to bypass both the `maxLiquidable` cap and the floor check — e.g., skip the `AmountGreaterThanMaxLiquidable` check when `amountToRepay_ == _debtTokenBalance`, and/or waive the floor check when `debtTokenBalance < debtFloorInUsd` equivalent so dust positions can always be fully closed. Alternatively, enforce the floor on `DebtToken.repay` so sub-floor debts can never be created.

### Proof of Concept
Hardhat/Foundry fork, replicating the existing test setup (`test/Pool.test.ts:408-436`):

```solidity
// Setup: pool with debtFloorInUsd = $3,000, maxLiquidable = 0.5e18 (50%)
await pool.updateDebtFloor(parseEther('3000'));
await pool.updateMaxLiquidable(parseEther('0.5'));

// 1. Attacker deposits collateral and mints debt just above floor, e.g. 1 msETH @ $4000
//    (passes the mint-side floor check)
// 2. Attacker repays so remaining debt < floor, e.g. repay 0.3 msETH -> remaining $2800 < $3000
await msEthDebtToken.connect(attacker).repay(attacker.address, repayAmount);

// 3. Price moves so the position is unhealthy.
// 4. Any liquidation attempt reverts:
//    - liquidate(..., fullDebt, ...)      -> AmountGreaterThanMaxLiquidable
//    - liquidate(..., partialDebt, ...)   -> RemainingDebtIsLowerThanTheFloor
//    - liquidate(..., quoteLiquidateMax(...), ...) -> RemainingDebtIsLowerThanTheFloor
vm.expectRevert(Pool.AmountGreaterThanMaxLiquidable.selector);
pool.liquidate(msEth, attacker, fullDebt, msdMET);

vm.expectRevert(Pool.RemainingDebtIsLowerThanTheFloor.selector);
pool.liquidate(msEth, attacker, halfDebt, msdMET);
```

Uncertainty noted: I could not fully verify whether `DebtToken.repay` enforces the floor (index returned only match counts for `DebtToken.sol`). If repay does enforce it, the same state is still reachable via interest accrual/oracle repricing pushing a healthy-above-floor debt's USD value below the floor, but the purely attacker-crafted path would then require a favorable price move rather than a single repay call.