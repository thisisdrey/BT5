### Title
Unrecoverable oracle-call revert in `resolve-pyth`/`resolve-dia` DOSes borrow, withdraw, and liquidation - (File: mainnet/contracts/market/v0-4-market.clar)

### Summary
`v0-4-market.clar` resolves all collateral/debt USD values through `call-pyth` and `call-dia`, both of which `unwrap!` the external oracle `contract-call?` into a hard revert (`ERR-ORACLE-PYTH` / `ERR-ORACLE-DIA`) with no `try/catch`-style fallback and no fixed-price override path, mirroring the root cause described in the referenced Inverse Finance report.

### Finding Description
`call-pyth` calls `pyth-storage-v4 get-price` and immediately `unwrap!`s the result, reverting the whole transaction with `ERR-ORACLE-PYTH` if the call errors: [1](#0-0) 

Similarly `call-dia` `unwrap!`s the DIA oracle call into `ERR-ORACLE-DIA`: [2](#0-1) 

These feed `resolve-pyth`/`resolve-dia`, which are dispatched by `resolve-price-feed`: [3](#0-2) 

`price-resolve` (and the batch variant `price-multi-resolve`/`iter-price-multi`) propagates any oracle failure via `try!`, with no fallback logic (no fixed price, no secondary oracle, no cached stale price): [4](#0-3) 

Unlike `docs/oracle.md`/`v0-1-data.clar`'s read-only helper `get-pyth-price`, which uses `match` and returns `none`/default `u0` on error rather than reverting: [5](#0-4) 

`v0-4-market.clar`'s on-chain accounting path (used for actual health checks, not just read-only views) hard-reverts instead of degrading gracefully. There is no code path in `v0-4-market.clar` that sets or consults a manually-configured fixed/fallback price before calling the oracle — the only price source for collateral/debt valuation used in health/liquidity calculations is `price-resolve`, which is 100% dependent on the external Pyth/DIA `contract-call?` succeeding.

### Impact Explanation
If the external Pyth (`pyth-storage-v4`) or DIA oracle contract is paused, upgraded incompatibly, hits `ERR_PRICE_FEED_NOT_FOUND`/`ERR_STALE_PRICE` in `pyth-storage-v4.clar`, or otherwise reverts/errors for any registered collateral or debt asset, then every market function that must value a user's position (borrow, withdraw, force-replenish, liquidate) reverts because `price-resolve`/`price-multi-resolve` cannot produce a price. This blocks depositors from withdrawing their collateral and blocks liquidators from liquidating undercollateralized positions during the outage — a temporary freezing of funds for as long as the oracle dependency is unavailable, which is squarely within the in-scope "temporary freezing of funds" impact class.

### Likelihood Explanation
This does not require any protocol bug to trigger — it is triggered purely by the external oracle's own availability, which is explicitly out of the attacker's control and is a well-documented failure mode for oracle dependencies (Chainlink multisig blocking access in the referenced report; analogous single point of failure here with Pyth's `pyth-storage-v4`/`pyth-governance-v3` gating and DIA). Given the protocol has exactly one oracle path per asset with no fallback, any transient unavailability of the upstream oracle immediately and completely blocks user-facing operations for all assets that share that oracle dependency.

### Recommendation
Add fallback logic in `call-pyth`/`call-dia` (or in `resolve-price-feed`) so that an oracle-call failure does not propagate as an uncatchable revert for the whole valuation pipeline — e.g., fall back to a manually-set, capped fixed price, a cached last-known-good price with staleness bounds, or a secondary oracle, mirroring the graceful-degradation pattern already used by the read-only `get-pyth-price`/`get-dia-price` helpers in `v0-1-data.clar`/`protocol-data.clar`. At minimum, provide a governance-settable fixed price per asset that can be enabled once the primary feed is confirmed down, so borrow/withdraw/liquidate can continue functioning.

### Proof of Concept
1. A collateral/debt asset in `v0-4-market.clar` uses `TYPE-PYTH` (or `TYPE-DIA`) with no alternative price source configured.
2. The external `pyth-storage-v4` (or DIA) contract becomes unavailable/errors for that asset's feed (e.g., governance pause, feed removal, stale-price rejection in `pyth-storage-v4.clar`'s `read-price-with-staleness-check`).
3. Any user calls a market function that needs `price-resolve`/`price-multi-resolve` for that asset (borrow, withdraw, force-replenish, liquidate).
4. `call-pyth`/`call-dia`'s `unwrap!` reverts the entire transaction with `ERR-ORACLE-PYTH`/`ERR-ORACLE-DIA`.
5. All such operations for that asset remain blocked for every user until the upstream oracle recovers, with no on-chain fallback available.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L308-310)
```text
(define-private (call-pyth (ident (buff 32)))
  (let ((res (unwrap! (contract-call? 'SP1CGXWEAMG6P6FT04W66NVGJ7PQWMDAC19R7PJ0Y.pyth-storage-v4 get-price ident) ERR-ORACLE-PYTH)))
    (ok res)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L312-335)
```text
(define-private (resolve-pyth (ident (buff 32)))
  (let ((response (try! (call-pyth ident)))
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

(define-private (resolve-price-feed (type (buff 1)) (ident (buff 32)))
  (if (is-eq type TYPE-PYTH) (resolve-pyth ident)
  (if (is-eq type TYPE-DIA) (resolve-dia ident)
  ERR-ORACLE-TYPE)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L373-418)
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

**File:** mainnet/contracts/utility/v0-1-data.clar (L91-94)
```text
(define-private (get-pyth-price (feed-id (buff 32)))
  (match (contract-call? 'SP1CGXWEAMG6P6FT04W66NVGJ7PQWMDAC19R7PJ0Y.pyth-storage-v4 get-price feed-id)
    result (some (normalize-pyth (get price result) (get expo result)))
    err-val none))
```
