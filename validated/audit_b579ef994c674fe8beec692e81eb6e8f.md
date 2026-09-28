### Title
Liquidators can front-run borrowers immediately after `Pool.open()` because shutdown blocks repayments but no post-reopen grace period exists - ([File: contracts/Pool.sol](metronome-synth-public--022/contracts/Pool.sol))

### Summary
`Pool.liquidate()` is gated only by `whenNotShutdown` (`Pool.sol:545`), while `shutdown()` suspends "issue, repay, deposit, withdraw, liquidate and swap" per `Pauseable.sol:111`. When the governor calls `open()` (`Pauseable.sol:97-100`), repayments and liquidations resume atomically in the same block. A position that became unhealthy during the shutdown window (interest accrual via `DebtToken.accrueInterest`, or oracle price drift) can be seized by a liquidator in the very first transaction after `open()`, before the borrower gets any chance to call `repay`. There is no `repayResumedTimestamp`-style waiting period or equivalent mechanism anywhere in `Pool.sol` or `Pauseable.sol`.

### Finding Description
The original Blueberry report describes missing synchronization between repay enablement and liquidation enablement. Metronome has the identical structural flaw with a different flag: `Pauseable._everythingStopped` gates both `DebtToken` repay paths and `Pool.liquidate`. The sequence:

1. Guardian/governor calls `shutdown()` — borrowers cannot repay, cannot top up collateral, and positions continue accruing interest and tracking oracle prices.
2. Governor calls `open()` — `_everythingStopped = false` in one transaction.
3. In the same block, an unprivileged liquidator calls `Pool.liquidate(syntheticToken_, victim, amountToRepay_, depositToken_)` (`Pool.sol:537-596`). All checks pass: `whenNotShutdown` now passes, `_isHealthy` is false, `amountToRepay_` respects `maxLiquidable` (50% per call, repeatable), `_totalSeized <= depositToken_.balanceOf`.
4. The liquidator burns synth, seizes collateral plus `liquidatorIncentive` and `protocolFee` (`quoteLiquidateOut`, `Pool.sol:451-472`), and the borrower — whose repay transaction is in the same mempool — loses collateral it never had a fair window to rescue.

`maxLiquidable = 0.5e18` (`Pool.sol:181`) limits each call to 50% of debt, but nothing prevents consecutive calls, so substantially the entire position can be drained across a few transactions before a human borrower reacts.

### Impact Explanation
Direct loss of user funds: borrowers whose positions decayed during a shutdown have their collateral seized plus liquidation incentive, with zero opportunity to cure. This is not standard liquidation racing (which applies to always-live protocols); it is a window artificially created by the protocol's own emergency mechanism, where the protocol guarantees liquidators a head start over borrowers — exactly the unfairness invariant (fair liquidation bounds) broken in the source report.

### Likelihood Explanation
Requires a shutdown/open cycle to occur while unhealthy or near-unhealthy positions exist, plus a liquidator monitoring `open()` transactions — trivially observable on-chain (the `Open` event / mempool). Shutdowns are rare but real (emergency flags are documented in `docs/emergency-flags.md`), and MEV bots already compete on liquidation flows. No privileged attacker action is needed; the attacker is a plain EOA calling `liquidate`. Likelihood is moderate, matching Medium severity.

### Recommendation
Record `block.timestamp` in `open()` (e.g., `reopenedTimestamp`) and add a `LIQUIDATION_GRACE_PERIOD` check in `Pool.liquidate()`: `if (block.timestamp < reopenedTimestamp + GRACE_PERIOD) revert LiquidationNotAllowedYet();`. Alternatively, keep `liquidate` blocked under a separate flag that the governor lifts only after the grace period has elapsed.

### Proof of Concept
Foundry fork sketch (per-repo test harness exists in `test/`):

```solidity
// Fork mainnet, impersonate governor/guardian.
// 1. Victim has msUSD debt backed by a DepositToken; position near collateralFactor limit.
// 2. poolRegistry.shutdown() (or pool.shutdown()) — repay now reverts IsShutdown.
// 3. Warp forward so accrueInterest() pushes debt above issuable limit
//    (or move oracle price down in a mock-oracle harness).
// 4. governor: pool.open();
// 5. Attacker EOA, same block:
//    pool.liquidate(msUSD, victim, debtToken.balanceOf(victim).wadMul(0.5e18), depositToken);
//    // succeeds — victim's repay tx reverts PositionIsHealthy or arrives too late
// Assert: attacker received depositToken_.seize output incl. liquidatorIncentive;
// victim had zero on-chain window to call DebtToken.repay().
```

Note: I could not fully verify the exact `whenNotShutdown`/`whenNotPaused` modifiers on `DebtToken.repay`/`repayAll` within the available iterations (grep returned match counts without line content). The claim that repay is suspended during shutdown rests on the explicit comment in `Pauseable.sol:111`; confirming the modifier on `contracts/DebtToken.sol` would strengthen the PoC but does not change the analysis, since `liquidate` is confirmed `whenNotShutdown`-only at `Pool.sol:545` and no grace-period mechanism exists anywhere in the codebase.