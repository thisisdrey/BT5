## Vulnerability Found

### Title
NAV computation stuck by an unresponsive `NavCalculator` permanently blocks `setCalculator` (and all `_requireIdleNav`-gated recovery), bricking deposits, redemptions, and loan operations - (File: `contracts/PortfolioVault.sol`)

### Summary
The reported ArtGobblers bug is a "stuck-flag" pattern: `requestRandomSeed()` sets `waitingForSeed = true`, and the only function that can clear it (`acceptRandomSeed`) is gated to the external `randProvider`. If that provider stops responding, the *recovery function* (`upgradeRandProvider`) also reverts because it too is gated on `!waitingForSeed`, permanently bricking reveals. `PortfolioVault.sol` has the same structural flaw around its NAV computation state machine: `navStart != 0` marks an in-progress, multi-block NAV cycle driven by the external `INavCalculator`. If the calculator stops responding partway through a cycle, `navStart` can never return to `0`, and the vault's own repair mechanism, `setCalculator`, is itself gated by `navStart == 0` — exactly mirroring the `waitingForSeed`/`upgradeRandProvider` deadlock.

### Finding Description
`PortfolioVault` runs NAV computation in batches across multiple transactions/blocks for gas-limit reasons [1](#0-0) . `navStart` is set to a non-zero timestamp when a NAV cycle begins and is only cleared back to `0` when the cycle *finalizes* successfully [2](#0-1) . Per-loan valuation during this cycle is delegated to the external `INavCalculator` contract [3](#0-2) .

The documented timeout/restart mechanism (`maxNavComputationTime`, ownership-nonce mismatch) only resets `navCursor`/`pendingNav` and re-emits `NavComputationStarted` — it does **not** reset `navStart` to `0`; it merely restarts the enumeration loop within the same still-open cycle [4](#0-3) . If the `NavCalculator` becomes permanently unresponsive (reverts, is bricked, or is upgraded incompatibly) mid-cycle — after `navStart` has already been committed to non-zero in an earlier successful batch transaction — every subsequent `updateNav()` call reverts when it tries to consult the calculator, and `navStart` can never be driven back to `0` by any restart path.

Critically, the vault's designed recovery mechanism for a broken calculator, `setCalculator`, is itself gated by `_requireIdleNav` (`navStart == 0`) [5](#0-4) . So once `navStart` is stuck non-zero because of the very calculator that `setCalculator` exists to replace, the guardian has no on-chain path to swap in a working calculator — the exact same "recovery function requires the state that only the broken dependency can clear" shape as `upgradeRandProvider` reverting on `SeedPending`.

The same `_requireIdleNav` gate also blocks `setLoans`, `setExchange`, `acceptSaleOffer`, `fundLoan`/`fundLoans`, `transferLoans`, `collectCashflows`, `addLoansToNav`, and `removeLoansFromNav` [5](#0-4) . In parallel, `_requireFreshNav` (used by `approveDeposit`/`approveRedemption`) explicitly reverts with `NavComputationInProgress` whenever `navStart != 0` [6](#0-5) , so once stuck, no new deposit or redemption can ever be approved again either.

### Impact Explanation
Once `navStart` is stuck, the vault permanently loses:
- All future `approveDeposit` / `approveRedemption` calls (`NavComputationInProgress` forever) — investor deposits and redemptions can never be approved again.
- All portfolio operations gated by `_requireIdleNav`: `fundLoan`/`fundLoans` (disbursing capital into new loans), `collectCashflows` (pulling investor cash out of loans), `acceptSaleOffer`/`transferLoans` (loan-bundle trading), and `addLoansToNav`/`removeLoansFromNav`.
- The one function meant to fix this — `setCalculator` — as well as `setLoans`/`setExchange`, which are also blocked, closing off migration to a fresh calculator/loans pair via the normal path.

This is a total, protocol-wide production DoS of NAV-sensitive approvals, loan funding/disbursement, cashflow collection, and loan settlement across all users — squarely within the allowed impact "Material production DoS or state corruption that blocks funding, disbursement, withdrawals, claims, NAV-sensitive approvals, or loan settlement across users." Recovery would require an emergency contract migration (deploying a whole new vault) rather than any built-in on-chain repair, since the intended repair function is itself bricked.

### Likelihood Explanation
Like the original finding, this is contingent on an externality (the external, mutable `INavCalculator` contract becoming unresponsive or reverting mid-cycle — e.g., due to a bug, an incompatible upgrade, or a contract-level failure), not on attacker action. Given large portfolios take many blocks to fully enumerate (44+ blocks for 50,000 loans per the doc's own gas table) [7](#0-6) , there is a real multi-block window during which the calculator dependency must remain available for every batch call, and any calculator malfunction landing inside that window immediately and irrecoverably strands `navStart`.

### Recommendation
Do not require `navStart == 0` for `setCalculator` (and ideally `setLoans`/`setExchange`). Instead, allow the guardian to force-abort/reset an in-progress NAV cycle (clear `navStart`, `navCursor`, `pendingNav`) as part of, or immediately before, `setCalculator`, mirroring the ArtGobblers fix of resetting the stuck state inside the recovery function rather than reverting on it. This preserves the guardian's ability to swap in a working calculator even while a NAV cycle is stuck, without permanently freezing vault operations.

### Proof of Concept
1. Vault has a large curated loan list requiring multiple `updateNav(batchSize)` calls across several blocks to finalize.
2. Manager calls `updateNav(batchSize)`; `navStart` is set to `block.timestamp` and the first batch completes successfully, advancing `navCursor` partway through `_navLoanIds`.
3. The `NavCalculator` contract becomes unresponsive (reverts unconditionally on every call — e.g. due to an internal bug, a broken upgrade, or exhausted external dependency) before the cycle can be finalized.
4. Every subsequent `updateNav()` call now reverts inside the calculator call, so `navStart` can never return to `0` (the `maxNavComputationTime` restart path only resets `navCursor`/`pendingNav`, not `navStart`).
5. `approveDeposit` / `approveRedemption` now permanently revert with `NavComputationInProgress`.
6. Guardian attempts `setCalculator(newCalculator)` to replace the broken calculator — this reverts because `_requireIdleNav` requires `navStart == 0`, which can never be true again.
7. The vault is permanently stuck: no new deposits/redemptions can be approved, no loans can be funded, no cashflows collected, and the intended repair path (`setCalculator`) is unusable.

### Citations

**File:** tare-io__tare-contracts/specs/vault.md (L209-209)
```markdown
- `approveDeposit` and `approveRedemption` enforce NAV freshness: they revert if `navStart != 0` (NAV computation in progress), if `lastNav == 0` (vault not yet bootstrapped), if `block.timestamp - lastNavUpdate > maxNavAge` (stale NAV), or if `calculator.configurationVersion() != lastCalculatorConfigurationVersion` (calculator configuration changed since the cached NAV was finalized — surfaced as `CalculatorConfigurationChanged`)
```

**File:** tare-io__tare-contracts/specs/vault.md (L217-217)
```markdown
- `calculator` — address of the `INavCalculator` contract used for loan valuation during NAV computation
```

**File:** tare-io__tare-contracts/specs/vault.md (L397-399)
```markdown
| Check                               | Functions                                                                                                                                                          |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `_requireIdleNav` (`navStart == 0`) | `acceptSaleOffer`, `fundLoan`, `fundLoans`, `transferLoans`, `collectCashflows`, `addLoansToNav`, `removeLoansFromNav`, `setCalculator`, `setLoans`, `setExchange` |
```

**File:** tare-io__tare-contracts/specs/vault.md (L429-429)
```markdown
To detect any such change, `LoansNFT` exposes a per-address monotonic `ownershipNonce(address)` mapping that is incremented inside its `_update` override (the single funnel for mints, transfers, and burns) once for the sender and once for the receiver. The vault snapshots `ownershipNonce(address(this))` at the start of each NAV cycle and re-reads it on every batch. If the nonce no longer matches, the in-progress computation is restarted: `navCursor` and `pendingNav` are reset, the new nonce is captured, and a `NavComputationStarted` event is re-emitted. The same restart branch covers the existing `maxNavComputationTime` timeout. Restarting (rather than reverting) lets the manager simply resume by calling `updateNav` again. Within each batch, entries whose `ownerOf` no longer matches the va ... (truncated)
```

**File:** tare-io__tare-contracts/specs/vault.md (L431-443)
```markdown
### Gas Costs and Scaling

Scaling on Avalanche (15M gas block limit, 2s/block).
The vault is chain-agnostic — these numbers are illustrative and should be benchmarked per deployment target.
_Numbers likely underestimated → to double check_

| Portfolio size | NAV computation (batch view) | NAV computation (no batch view) |
| -------------- | ---------------------------- | ------------------------------- |
| 5,000 loans    | 4-5 blocks (~10s)            | 7 blocks (~14s)                 |
| 50,000 loans   | 44 blocks (~1.5 min)         | 70 blocks (~2.3 min)            |
| 100,000 loans  | 87 blocks (~2.9 min)         | 139 blocks (~4.6 min)           |

Every NAV computation iterates the entire portfolio.
```

**File:** tare-io__tare-contracts/specs/vault.md (L742-746)
```markdown
uint256 public navCursor;              // Index into _navLoanIds for pagination
uint256 public pendingNav;             // Running total of loan values during computation
uint256 public navStart;               // Timestamp when current NAV computation started (0 when idle)
uint256 public lastNav;                // Final NAV value from most recent completed computation
uint256 public lastNavUpdate;          // Timestamp of the most recent completed NAV computation
```
