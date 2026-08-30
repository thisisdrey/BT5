### Title
Stale per-block `index-cache` in `market.clar` can be replayed after `socialize-debt` writes down a vault's `lindex`, causing zToken collateral to be over-valued for the rest of the block - (File: `mainnet/contracts/market/v0-4-market.clar`, `mainnet/contracts/vault/v0-vault-stx.clar`)

### Summary
The reported bug class ("a cache that times out can be recovered") maps to Zest's per-block `index-cache` in `market.clar`. That cache stores `{index, lindex}` per `{timestamp, aid}` and is meant to reflect the freshest accrued vault state for the current block. However, `socialize-debt` in the vault contracts mutates `lindex`/`index`-derived state directly, bypassing `accrue-and-cache`, so a value already cached earlier in the same block is never invalidated and gets replayed as if still valid — mirroring the `LocalCache#set_expire`/`get` inconsistency in the external report.

### Finding Description
`market.clar` caches vault indexes keyed by `{timestamp: stacks-block-time, aid}` via `accrue-and-cache`: [1](#0-0) 

This is a cache-hit/cache-miss pattern functionally identical to `LocalCache#get`/`set_expire`: once a value is written for the current block's timestamp key, any later read in the same block returns the cached value without re-validating it against the vault's live state.

The vault's `socialize-debt` function, however, updates `lindex` (and other state) directly and does **not** go through `accrue`/`accrue-and-cache`, nor does it touch `market.clar`'s `index-cache`: [2](#0-1) 

So the sequence is:
1. Earlier in a block/transaction, some operation (deposit, borrow, health check, price resolution) calls `accrue-and-cache(aid)`, which populates `index-cache` with the vault's current `{index, lindex}` for `{timestamp: stacks-block-time, aid}`.
2. A liquidation that produces bad debt triggers `vault-socialize-debt`, which writes down `lindex` in the vault (representing a real loss to `zToken` value) — but the market-level cache entry for that `aid`/timestamp is left untouched, still holding the pre-loss `lindex`.
3. Any subsequent operation in the *same block* (e.g., `resolve-ztoken` pricing a `zToken` used as collateral, or another position's health check) calls `accrue-and-cache(aid)` again, hits the stale cache entry, and uses the pre-loss `lindex` to price `zToken` collateral: [3](#0-2) 

This is the same root cause as the report: the cache's staleness/expiry is tied to a coarse invalidation boundary (per-timestamp key) rather than being invalidated whenever the underlying value it caches actually changes, so state-changing operations that bypass the cache-population path (here, `socialize-debt`) leave a "recoverable" stale entry that gets served as fresh.

### Impact Explanation
`zToken` collateral value (`resolve-ztoken`) and any health/liquidation computation relying on the cached `lindex` for the affected vault continue to use the pre-write-down index for the remainder of the block. This can let positions collateralized with the affected `zToken` avoid liquidation, or let a user withdraw/borrow against an inflated collateral valuation, within the same block that debt was socialized against that vault. This lands on temporary freezing of funds/insolvency-adjacent mispricing in the protocol accounting (the position/collateral accounting momentarily diverges from actual vault backing), matching the in-scope "vault share math and interest accrual" / "socialize-debt" / "per-block index cache" categories.

### Likelihood Explanation
`socialize-debt` is only reachable through the liquidation path when bad debt is realized, so triggering it requires an actual undercollateralized position and a liquidation — not privileged access. Once triggered, exploiting the stale cache only requires a second, ordinary operation (borrow, deposit, or a query that resolves `zToken` price) to occur within the same block/timestamp before the vault's `lindex` naturally re-syncs on the next `accrue-and-cache` call for a new timestamp. This is plausible on any chain where multiple transactions can share `stacks-block-time`, and does not require any DAO or oracle compromise.

### Recommendation
Invalidate (or update) `market.clar`'s `index-cache` entry for the affected `aid`/current timestamp whenever `vault-socialize-debt` runs, e.g., by having `vault-socialize-debt` return the post-write-down `{index, lindex}` and having `market.clar` `map-set index-cache` with that fresh value immediately after calling `vault-socialize-debt`, instead of leaving the previously cached entry in place.

### Proof of Concept
1. Within a block, call any market operation on asset `aid` that triggers `accrue-and-cache(aid)` (e.g., a deposit into the vault), populating `index-cache` with `{index: I0, lindex: L0}` for `{timestamp: T, aid}`.
2. In the same block, trigger a liquidation on an undercollateralized position of `aid` that results in bad debt, causing `market.clar` to call `vault-socialize-debt`, which writes `lindex` down to `L1 < L0` in the vault: [4](#0-3) 
3. In the same block (same `T`), perform another operation that needs the `zToken` price for `aid` (e.g., health check or borrow against `zToken` collateral); `resolve-ztoken` calls `get-cached-indexes`/`accrue-and-cache`, which hits the cache from step 1 and returns stale `lindex = L0` instead of the corrected `L1`, over-valuing the `zToken` collateral for that operation.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L245-257)
```text
(define-private (accrue-and-cache (aid uint))
  (let ((cache-key { timestamp: stacks-block-time, aid: aid })
        (cached? (map-get? index-cache cache-key)))

    (match cached?
      ;; cache HIT: return cached value (1 read only)
      cached-indexes (ok cached-indexes)

      ;; cache MISS: accrue and cache (vault-accrue now returns indexes)
      (let ((indexes (try! (vault-accrue aid))))
        ;; store in cache
        (map-set index-cache cache-key indexes)
        (ok indexes)))))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L343-347)
```text
(define-private (resolve-ztoken (p uint) (aid uint))
  (let ((cached (unwrap! (get-cached-indexes aid) ERR-ORACLE-CALLCODE))
        (cached-lindex (get lindex cached))
        (scaled (* p cached-lindex)))
    (ok (div-down scaled INDEX-PRECISION))))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L944-984)
```text
(define-public (socialize-debt (scaled-amount uint))
  (let ((scaled-principal (var-get principal-scaled))
        (borrowed (var-get total-borrowed))
        (idx (var-get index))
        (current-assets (var-get assets))
        (current-lindex (var-get lindex))
        (old-total-assets (total-assets))
        (debt-reduction (mul-div-down scaled-amount idx INDEX-PRECISION))
        (principal-reduction (if (> scaled-principal u0)
                                (mul-div-down scaled-amount borrowed scaled-principal)
                                u0))
        ;; Write down lindex proportionally to loss in total-assets
        (new-lindex (if (and (> old-total-assets u0) (> old-total-assets debt-reduction))
                       (mul-div-down current-lindex (- old-total-assets debt-reduction) old-total-assets)
                       u0)))

    (try! (check-caller-auth))
    (asserts! (> scaled-amount u0) ERR-AMOUNT-ZERO)

    (var-set lindex new-lindex)
    (var-set principal-scaled (if (> scaled-principal scaled-amount) (- scaled-principal scaled-amount) u0))
    (var-set total-borrowed (if (> borrowed principal-reduction) (- borrowed principal-reduction) u0))
    (var-set assets (if (> current-assets principal-reduction) (- current-assets principal-reduction) u0))

    (print {
      action: "socialize-debt",
      caller: contract-caller,
      data: {
        scaled-amount: scaled-amount,
        debt-reduction: debt-reduction,
        principal-reduction: principal-reduction,
        old-lindex: current-lindex,
        new-lindex: new-lindex,
        old-total-assets: old-total-assets,
        principal-scaled: (if (> scaled-principal scaled-amount) (- scaled-principal scaled-amount) u0),
        total-borrowed: (if (> borrowed principal-reduction) (- borrowed principal-reduction) u0),
        index: idx
      }
    })

    (ok true)))
```
