The analog to this Sherlock finding exists in Zest's market contract, though it maps to `check-dao-auth`-gated liquidation grace-period configuration rather than a strategy-management role.

### Title
Missing maximum bound on `set-liquidation-grace-period` / `set-pause-liquidation` grace period allows indefinite blocking of liquidations, permanently freezing lender funds - (File: `mainnet/contracts/market/v0-4-market.clar`)

### Summary
`set-liquidation-grace-period` and the grace-period branch of `set-pause-liquidation` accept an arbitrary `grace-period` value and store `stacks-block-time + grace-period` into `liquidation-grace-periods` with no upper bound check, directly gating whether `liquidate` can execute for an asset.

### Finding Description
`set-liquidation-grace-period` writes `(+ stacks-block-time grace-period)` into the `liquidation-grace-periods` map for an arbitrary asset id, with only a `check-dao-auth` gate and no sanity limit on `grace-period` itself: [1](#0-0) 

This value is consumed by `is-liquidation-paused`, which blocks `liquidate` for as long as `stacks-block-time < asset-grace-end` (or the global grace end): [2](#0-1) 

`liquidate` itself enforces this pause check as a hard requirement before any liquidation logic executes: [3](#0-2) 

The `set-pause-liquidation` grace-period branch has the identical unbounded pattern, only applied on unpause transitions: [4](#0-3) 

There is no maximum cap comparable to the bounds used elsewhere in the same contract for other DAO-set risk parameters (e.g. `set-max-confidence-ratio` enforces `<= BPS`), so an arbitrarily large `grace-period` (up to `uint` max) can be supplied, extending the liquidation block for that asset (or globally) indefinitely — functionally permanent.

### Impact Explanation
While liquidations are blocked, borrowers with unhealthy positions cannot be liquidated. Debt continues to accrue against depreciating/under-collateralized collateral, and lenders supplying the affected vault's underlying asset cannot recover principal that depends on liquidation proceeds or `socialize-debt` cleanup, since `liquidate` is the only path that repays vault debt from an unhealthy position via `vault-system-repay`. With no maximum on `grace-period`, this can freeze lender funds for the entire lifetime of the grace period, which can be set effectively unbounded — this is a temporary (or, with a sufficiently large value, effectively permanent) freezing of funds impact.

### Likelihood Explanation
The functions are reachable directly by any principal empowered by DAO governance (`check-dao-auth`) without any additional argument validation on `grace-period` — a single call with a large value is sufficient. This mirrors the reported bug class exactly: a legitimate configuration setter with a minimum-safety intent (allow positions to stabilize post-unpause) but no maximum bound, enabling the same privileged role that is expected to use it benignly to instead lock funds by mistake or overreach, exactly as in the referenced `dustThreshold` report.

### Recommendation
Add an explicit maximum bound (e.g., a `MAX-GRACE-PERIOD` constant) and assert `grace-period <= MAX-GRACE-PERIOD` in both `set-liquidation-grace-period` and the grace-period branch of `set-pause-liquidation`, analogous to the existing `<= BPS` bound used in `set-max-confidence-ratio`.

### Proof of Concept
1. DAO governance calls `set-liquidation-grace-period(id, grace-period)` with `grace-period = u340282366920938463463374607431768211455` (max uint) for a target asset id. [1](#0-0) 
2. `liquidation-grace-periods[id]` is set to `stacks-block-time + grace-period`, effectively unreachable in the future.
3. Any subsequent `liquidate` call against a borrower using that asset as debt reverts with `ERR-LIQUIDATION-PAUSED` because `is-liquidation-paused` evaluates true indefinitely. [2](#0-1) 
4. Unhealthy positions accumulate bad debt with no liquidation path, freezing recoverable value for vault depositors of that asset.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L691-697)
```text
(define-private (is-liquidation-paused (asset-id uint))
  (let ((manual-pause (var-get pause-liquidation))
        (global-grace-end (default-to u0 (map-get? liquidation-grace-periods GLOBAL-LIQUIDATION-GRACE-ID)))
        (asset-grace-end (default-to u0 (map-get? liquidation-grace-periods asset-id)))
        (global-grace-active (< stacks-block-time global-grace-end))
        (asset-grace-active (< stacks-block-time asset-grace-end)))
    (or manual-pause global-grace-active asset-grace-active)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L953-976)
```text
(define-public (set-pause-liquidation (paused bool) (grace-period uint))
  (begin
    (try! (check-dao-auth))
    (let ((was-paused (var-get pause-liquidation)))
      (var-set pause-liquidation paused)
      ;; Only set grace period if liquidations were paused AND now unpausing
      (if (and was-paused (not paused))
          (map-set liquidation-grace-periods GLOBAL-LIQUIDATION-GRACE-ID (+ stacks-block-time grace-period))
          false)
      
      (print {
        action: "market-set-pause-liquidation",
        caller: tx-sender,
        data: {
          was-paused: was-paused,
          now-paused: paused,
          grace-period: grace-period,
          grace-end: (if (and was-paused (not paused))
                         (+ stacks-block-time grace-period)
                         u0)
        }
      })
      
      (ok true))))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L978-993)
```text
(define-public (set-liquidation-grace-period (id uint) (grace-period uint))
  (begin
    (try! (check-dao-auth))
    (map-set liquidation-grace-periods id (+ stacks-block-time grace-period))
    
    (print {
      action: "market-set-liquidation-grace-period",
      caller: tx-sender,
      data: {
        asset-id: id,
        grace-period: grace-period,
        grace-end: (+ stacks-block-time grace-period)
      }
    })
    
    (ok true)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1488-1488)
```text
    (asserts! (not (is-liquidation-paused debt-aid)) ERR-LIQUIDATION-PAUSED)
```
