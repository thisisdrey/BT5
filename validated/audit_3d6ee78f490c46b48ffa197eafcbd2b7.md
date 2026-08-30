### Title
Single-asset oracle staleness/invalidity panics `get-assets`/`process-collateral-asset` and DOS's `liquidate`, `withdraw`, and `borrow` for any position holding that asset - (File: `mainnet/contracts/market/v0-4-market.clar`)

### Summary
`get-assets` and `process-collateral-asset` resolve collateral/debt prices via `price-multi-resolve`/`price-resolve` but wrap the results in `unwrap-panic` instead of a recoverable error. If a single asset's price feed fails the freshness or legality check, the panic aborts the *entire* calling transaction — including `liquidate` — exactly mirroring the BlueberryBank root cause where one unpriceable token DOSes the whole position valuation used for liquidation.

### Finding Description
`price-resolve` validates each price with `oracle-price-legal` (price must be `> 0`) and `oracle-timestamp-fresh` (delta since the feed's timestamp must be `<= max-staleness`), returning `ERR-ORACLE-INVARIANT` if either check fails: [1](#0-0) 

`get-assets` builds the full list of prices for a position's enabled-collateral mask by calling `price-multi-resolve` and then unconditionally unwrapping the result with `unwrap-panic`: [2](#0-1) 

`price-multi-resolve`/`iter-price-multi` themselves already propagate a soft `err` (`ERR-ORACLE-MULTI`) if *any single* asset in the list fails to resolve: [3](#0-2) 

But `get-assets` discards that recoverability by calling `unwrap-panic` on it, meaning a single stale or illegal-priced asset panics the entire contract call rather than surfacing a catchable error.

The same pattern exists in `process-collateral-asset`, used during liquidation for disabled collateral, which resolves the price on demand with `unwrap-panic`: [4](#0-3) 

`liquidate` calls `get-assets` (via `get-notional-evaluation`) over the borrower's enabled-collateral `mask`, and also calls `process-collateral-asset` for the seized collateral asset: [5](#0-4) [6](#0-5) 

Because a position's `mask` can hold several collateral assets simultaneously, if any one of those assets' oracle feed goes stale (publisher outage, feed not updated within `max-staleness`) or reports an illegal (`<= 0`) price, `price-resolve` returns `err ERR-ORACLE-INVARIANT`, `get-assets`/`process-collateral-asset` panic on the `unwrap-panic`, and the whole `liquidate` call for that borrower's position aborts — exactly like BlueberryBank's `getPositionValue` reverting entirely because one reward token had no oracle. The same `get-assets` call is also used by `borrow`, `collateral-remove`, and other user flows, so the same position becomes unable to be modified/liquidated while the affected asset's feed is unhealthy.

### Impact Explanation
An unhealthy position holding an asset whose price feed is temporarily stale or invalid cannot be liquidated for the duration of that condition, because `liquidate` unconditionally panics instead of gracefully skipping/erroring on that one asset. In a volatile market this blocks liquidators from seizing collateral while debt value moves further underwater, risking bad debt / protocol insolvency exposure and constituting a temporary freezing of the position's funds (collateral becomes non-liquidatable) — matching the High-severity "temporary freezing of funds" impact class from the analogous report.

### Likelihood Explanation
This does not require any privileged/DAO action — it is triggered purely by normal, expected operational conditions of the price oracle system already built into this code (a Pyth/DIA feed simply failing to update within the position's configured `max-staleness`, or reporting a non-positive price). Any position with multiple enabled collateral assets is exposed as soon as one of those assets' feed goes stale, making this reachable through ordinary `liquidate`/`borrow`/`collateral-remove` calls by any principal.

### Recommendation
Replace `unwrap-panic` on `price-multi-resolve` in `get-assets` and on `price-resolve` in `process-collateral-asset` with recoverable error propagation (`try!`/`unwrap!` returning a proper `err`) so callers such as `liquidate` can either (a) fail fast with a catchable error that still allows retry once the feed recovers, or (b) exclude/zero-value the unpriceable asset instead of aborting the whole position valuation, preserving liquidation availability for the rest of the position's assets.

### Proof of Concept
1. A borrower opens a position with two enabled collateral assets, A and B, plus outstanding debt.
2. Asset B's oracle feed (Pyth or DIA) stops updating past its configured `max-staleness`, or momentarily reports a `0`/negative price (a normal operational occurrence the `oracle-price-legal`/`oracle-timestamp-fresh` checks in `price-resolve` are explicitly designed to catch, see `mainnet/contracts/market/v0-4-market.clar:362-388`).
3. The position's LTV crosses the liquidation threshold (asset A's price alone would still make it unhealthy).
4. Any liquidator calls `liquidate`, which calls `get-assets(mask)` over both A and B; `price-multi-resolve` returns `err ERR-ORACLE-MULTI` due to asset B, and `get-assets`'s `unwrap-panic` (`mainnet/contracts/market/v0-4-market.clar:491`) causes the whole call to panic and abort.
5. The position cannot be liquidated until asset B's feed recovers, even though asset A's collateral alone should already allow (partial) liquidation — allowing bad debt to accumulate in a volatile market.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L362-395)
```text
(define-private (oracle-price-legal (p uint))
  (> p u0))

(define-private (oracle-timestamp-fresh (ts uint) (prev uint) (max-staleness uint))
  (let ((delta (if (> ts stacks-block-time)
                   u0
                   (- stacks-block-time ts))))
    (and
      (<= delta max-staleness)
      (>= ts prev))))

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

**File:** mainnet/contracts/market/v0-4-market.clar (L397-419)
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

**File:** mainnet/contracts/market/v0-4-market.clar (L808-815)
```text
        ;; Handle disabled collaterals by resolving price if not in enabled assets
        (coll-asset-info (match (find-asset coll-aid assets)
                           ;; Found in enabled list: use it (already has price)
                           found found
                           ;; Not found (disabled): resolve price on demand
                           (let ((oracle-data (get oracle coll-asset))
                                 (price (unwrap-panic (price-resolve oracle-data))))
                             (merge coll-asset { price: price }))))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1409-1420)
```text
    ;; NOW safe to resolve prices (cache is populated)
    (assets (get-assets mask))
    (notional-valued-assets (get-notional-evaluation { position: position, assets: assets }))
    (total-collateral-usd (get collateral notional-valued-assets))
    (total-debt-usd (get debt notional-valued-assets))

    ;; LTC thresholds, liq params, health
    (ltv-liq-partial (buff-to-uint-be (get LTV-LIQ-PARTIAL group)))
    (ltv-liq-full (buff-to-uint-be (get LTV-LIQ-FULL group)))
    (liq-penalty-min (buff-to-uint-be (get LIQ-PENALTY-MIN group)))
    (liq-penalty-max (buff-to-uint-be (get LIQ-PENALTY-MAX group)))
    (curve-exponent (buff-to-uint-be (get LIQ-CURVE-EXP group)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1453-1461)
```text
    ;; collateral processing
    (user-coll-balance (find-collateral-amount (get collateral pos-full) coll-aid))
    (coll-info (process-collateral-asset coll-aid debt-actual-usd liq-penalty 
                                         user-coll-balance assets coll-asset))
    (coll-actual (get coll-actual coll-info))
    (coll-expected (get coll-expected coll-info))
    (coll-price (get coll-price coll-info))
    (coll-decimals (get coll-decimals coll-info))

```
