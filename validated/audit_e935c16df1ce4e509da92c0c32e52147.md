Based on the code I've found, I now have enough evidence to identify a valid analog.

### Title
Failure to Resolve the Price of a Single Enabled Collateral or Debt Asset Blocks Withdrawal of All Other, Unrelated Collateral - ([File: mainnet/contracts/market/v0-4-market.clar])

### Summary
`collateral-remove` (the withdrawal entry point for a user's collateral in this hub-and-spoke lending protocol) requires computing the notional USD value of a borrower's *entire* enabled position before it allows removal of a *single* specific asset, whenever the position carries any debt. This aggregate valuation is computed by `get-assets`, which calls `price-multi-resolve` over *every* enabled collateral/debt asset the user holds, and that call reverts the whole withdrawal transaction if the oracle price of even one unrelated asset in the position cannot be resolved (stale, zero, confidence too low, or the external Pyth/DIA call reverts). This mirrors the reported bug class: a single point of failure inside a loop over multiple "child" operations blocks an otherwise-independent user withdrawal.

### Finding Description
`collateral-remove` in `v0-4-market.clar` (and the identically-structured `market.clar`) only allows an isolated fast-path (no price resolution) when the user has **no debt** at all: [1](#0-0) 

Once the user has any debt, the function must compute `collateral-value`/`debt-value` for the *whole* position via `get-notional-evaluation`, which is fed by `get-assets`: [2](#0-1) 

`get-assets` batches every enabled asset in the user's position mask and resolves all of their oracle prices together via `price-multi-resolve`: [3](#0-2) 

`price-multi-resolve` folds `iter-price-multi` over the asset list; the very first `unwrap!` failure marks the whole result as `valid: false`, and `price-multi-resolve` then `asserts!`s on `valid`, reverting the entire call (and thus the parent `collateral-remove` transaction) if resolving the price of **any single asset** in the loop fails: [4](#0-3) 

`price-resolve` itself reverts (`ERR-ORACLE-INVARIANT`, `ERR-ORACLE-PYTH`, `ERR-ORACLE-DIA`, `ERR-PRICE-CONFIDENCE-LOW`, etc.) if the underlying Pyth/DIA price is stale relative to that asset's own `max-staleness`, has too-low confidence, is non-positive, or if the external oracle contract call itself errors: [5](#0-4) 

Consequently, a user who holds, e.g., zSTX collateral (healthy, freshly-priced) and a small amount of a second collateral asset (say sBTC) whose Pyth feed happens to be stale/paused/confidence-degraded at that moment cannot withdraw *any* of their zSTX collateral — even though sBTC is completely unrelated to the requested withdrawal — because `get-assets`/`price-multi-resolve` aggregates and reverts on the first failing asset in the loop. This is directly analogous to the reported `PrimeStrategy.withdraw()` issue where a revert from one item inside a loop (there: one strategy in a priority queue; here: one asset's oracle resolution in the position) blocks the entire withdrawal operation for the user, with no `try/catch`/skip-and-continue fallback.

### Impact Explanation
This causes temporary freezing of funds: any user with debt and more than one enabled collateral/debt asset is unable to withdraw collateral they are otherwise fully entitled to and would be healthy without, purely because an unrelated asset's oracle feed is temporarily unavailable/stale/low-confidence. This matches the in-scope "temporary freezing of funds" impact class. Recovery requires waiting for the oracle to recover or for governance/DAO to intervene (e.g., disable the asset), which is outside the ordinary user's control — the exact "manual intervention dependency" flagged as the core problem in the referenced report.

### Likelihood Explanation
Oracle staleness/confidence failures are a normal, expected operational condition (network congestion, publisher downtime, temporary confidence degradation) rather than a contrived edge case, and any user simultaneously supplying/borrowing more than one asset is exposed. No malicious action or DAO compromise is required — this is purely a consequence of the current aggregate-valuation design in `get-assets`/`price-multi-resolve`, making the likelihood moderate-to-high in practice for multi-asset positions during oracle disruption windows.

### Recommendation
Decouple the withdrawal health-check price resolution from unrelated assets: either (a) resolve/require prices only for the assets that participate in the current withdrawal's health-check inputs (the asset being removed plus assets actually needed for LTV, rather than the full enabled mask), or (b) make `price-multi-resolve`/`iter-price-multi` tolerant of individual asset failures — e.g., allow excluding a stale/failing asset from the aggregate (treating it conservatively, such as valuing it at zero for collateral and skipping it if not relevant to debt) rather than reverting the entire fold on the first failure, mirroring the `try/catch`-and-skip recommendation from the original report.

### Proof of Concept
1. User has collateral in two enabled assets, e.g. zSTX (fresh price) and sBTC (small amount), and outstanding debt in USDC, keeping their position well within LTV limits considering only zSTX and USDC.
2. sBTC's Pyth feed publish becomes stale beyond its configured `max-staleness`, or its confidence ratio exceeds `max-confidence-ratio` (a normal operational event, not an attack), causing `resolve-pyth`/`price-resolve` to revert for sBTC. [6](#0-5) 
3. User calls `collateral-remove` to withdraw part of their zSTX collateral. Because they have debt, the function calls `get-assets`, which calls `price-multi-resolve` over the full enabled mask, including sBTC.
4. `iter-price-multi` hits the sBTC price-resolve failure, sets `valid: false`; `price-multi-resolve` then `asserts!`s and reverts with `ERR-ORACLE-MULTI`, aborting `collateral-remove` entirely.
5. The user cannot withdraw any of their healthy, unrelated zSTX collateral until sBTC's oracle recovers, demonstrating the temporary freeze caused by a single failing asset in the multi-asset valuation loop.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L314-330)
```text
        (price (get price response))
        (expo (get expo response))
        (conf (get conf response))
        (final-price (normalize-pyth price expo))
        (timestamp (get publish-time response)))
    (try! (check-confidence price conf))
    (ok { value: final-price, timestamp: timestamp })))

(define-private (call-dia (key (string-ascii 32)))
  (let ((res (unwrap! (contract-call? 'SP1G48FZ4Y7JY8G2Z0N51QTCYGBQ6F4J43J77BQC0.dia-oracle get-value key) ERR-ORACLE-DIA)))
    (ok res)))

(define-private (resolve-dia (ident (buff 32)))
  (let ((key (unwrap-panic (from-consensus-buff? (string-ascii 32) ident)))
        (res (try! (call-dia key))))
    ;; DIA returns timestamp in milliseconds, convert to seconds for staleness check
    (ok { value: (get value res), timestamp: (/ (get timestamp res) u1000) })))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L373-395)
```text
(define-private (price-resolve
  (data { type: (buff 1), ident: (buff 32), callcode: (optional (buff 1)), max-staleness: uint }))
  (let ((type (get type data))
        (ident (get ident data))
        (key { type: type, ident: ident })
        (resolution (try! (resolve-price-feed type ident)))
        (price (get value resolution))
        (callcode (get callcode data))
        (final-price (try! (resolve-callcode price callcode)))
        (last-update-time (oracle-last-update key))
        (timestamp (get timestamp resolution))
        (max-staleness (get max-staleness data)))

    ;; validate price and timestamp using max-staleness from oracle data
    (asserts! (and (oracle-price-legal final-price) (oracle-timestamp-fresh timestamp last-update-time max-staleness))
              ERR-ORACLE-INVARIANT)

    ;; update timestamp if newer
    (if (> timestamp last-update-time)
        (map-set last-update key timestamp)
        false)

    (ok final-price)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L397-418)
```text
(define-private (price-multi-resolve
  (data (list 64 { type: (buff 1), ident: (buff 32), callcode: (optional (buff 1)), max-staleness: uint }))
  (aids (list 64 uint)))
  (let ((init { output: (list), valid: true, aids: aids, idx: u0 })
        (response (fold iter-price-multi data init)))
    (asserts! (get valid response) ERR-ORACLE-MULTI)
    (ok (get output response))))

(define-private (iter-price-multi
  (oracle-data { type: (buff 1), ident: (buff 32), callcode: (optional (buff 1)), max-staleness: uint })
  (acc { output: (list 64 uint), valid: bool, aids: (list 64 uint), idx: uint }))
  (let ((valid (get valid acc))
        (skip? (asserts! valid acc))
        (asset-ids (get aids acc))
        (idx (get idx acc))
        ;; resolve price - will use cache for ztokens
        (price (unwrap! (price-resolve oracle-data) (merge acc { valid: false })))
        (next (unwrap-panic (as-max-len? (append (get output acc) price) u64))))
    { output: next,
      valid: true,
      aids: asset-ids,
      idx: (+ idx u1) }))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L482-492)
```text
(define-private (get-assets (mask-user uint))
  (let ((mask-enabled (get-enabled-bitmap))
        (safe-mask (user-safe-mask mask-user mask-enabled))
        (iter (mask-to-list-collateral safe-mask))
        (assets-list (get-status-multi iter))
        (oracles-list (map get-oracle assets-list))
        ;; Extract asset-ids for price resolution
        (asset-ids (map get-asset-id assets-list))
        ;; Use internal price resolution
        (prices-list (unwrap-panic (price-multi-resolve oracles-list asset-ids))))
    (map merge-price assets-list prices-list)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1114-1120)
```text
        (has-debt (> (len (get debt position)) u0)))

    (asserts! (> amount u0) ERR-AMOUNT-ZERO)

    (if has-debt
        ;; HAS DEBT: Full flow with price resolution and health checks
        (let ((is-collateral-enabled (get collateral asset))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1123-1134)
```text
              (pos-full (if is-collateral-enabled position (try! (get-full-position account))))
              (u-debt (accrue-user-debts (get debt pos-full)))
              (u-coll (accrue-user-collateral (get collateral pos-full)))
              (assets (get-assets position-mask))
              (curr-coll-aid (find-collateral-amount (get collateral position) asset-id))
              (removing-all (is-eq amount curr-coll-aid))
              (current-group (try! (get-egroup position-mask)))
              (current-ltvb (buff-to-uint-be (get LTV-BORROW current-group)))
              (notional-valued-assets (get-notional-evaluation { position: position, assets: assets }))
              (collateral-value (get collateral notional-valued-assets))
              (debt-value (get debt notional-valued-assets))
              (removed-asset-value (find-and-resolve-asset-value assets asset-id amount true)))
```
