Found it: `socialize-debt` in the vault contracts directly mutates `lindex` (and `principal-scaled`/`total-borrowed`/`assets`) without going through `accrue` first, and it bypasses the market's `index-cache-` entirely.

### Title
Stale cached liquidity index after `socialize-debt` causes ztoken/collateral mispricing within the same block - ([File: mainnet/contracts/market/v0-4-market.clar], [File: mainnet/contracts/vault/v0-vault-stx.clar])

### Summary
The market contract caches vault `index`/`lindex` per `(timestamp, aid)` in `index-cache` and only recomputes on a cache miss [1](#0-0) . `socialize-debt` in the vault contracts directly overwrites `lindex` (write-down proportional to loss) outside of the normal `accrue` flow and without any market-side cache invalidation [2](#0-1) . If a socialize-debt event and a subsequent ztoken price resolution or collateral/health calculation for the same asset happen within the same `stacks-block-time`, the market will serve the pre-write-down `lindex` from `index-cache` rather than the freshly written value.

### Finding Description
`accrue-and-cache` treats a cache hit as authoritative for the remainder of the block: `(match cached? existing existing ...)` [3](#0-2) . This is safe under the assumption that the only way `index`/`lindex` change is through `accrue`, which is always invoked before reading the vars and is what populates the cache. However `socialize-debt` writes `lindex` directly via `var-set lindex new-lindex` [4](#0-3) , without calling through `accrue-and-cache`, and without any mechanism to invalidate or update the market's `index-cache` entry for that `(timestamp, aid)` key. The same pattern (`socialize-debt` writing `lindex` directly, uncoordinated with the market cache) is duplicated across all vaults (`v0-vault-sbtc`, `v0-vault-ststx`, `v0-vault-ststxbtc`, `v0-vault-usdc`, `v0-vault-usdh`) [5](#0-4) .

Consequently, in a scenario where within a single block: (1) some earlier transaction in the block causes the market to call `accrue-and-cache` for asset `aid` and populate `index-cache- {timestamp, aid}` with the pre-loss `lindex`, and (2) `socialize-debt` is subsequently invoked on the same vault (writing down `lindex` to reflect the loss), then (3) a later transaction in the same block that prices the corresponding ztoken as collateral or computes debt/health for that asset calls `accrue-and-cache` again — the market returns the stale cached `lindex`, not the vault's actual (written-down) `lindex`.

### Impact Explanation
Because ztoken collateral value is derived from the cached `lindex` (documented as "Uses cached indexes ... for ztoken price resolution" [6](#0-5) ), an over-valued stale index would let a position appear over-collateralized relative to its true post-loss value, permitting new borrows or preventing correct liquidation against real collateral value during that block — theft of funds at rest (protocol insolvency risk) since debt could be extended or collateral released against a mispriced (overstated) ztoken. This lands in the Critical impact bucket (protocol insolvency / theft of funds via mispriced collateral), since the divergence between actual and reported vault state directly affects collateral/debt accounting used for market entry points and health checks.

### Likelihood Explanation
This requires `socialize-debt` (a caller-authorized, non-DAO-gated operational function reachable via `check-caller-auth`) to be executed within the same block/timestamp as prior and subsequent market interactions touching the same asset's index cache — a realistic sequencing during any liquidation cascade or bad-debt socialization event, which is precisely when accurate collateral valuation matters most.

### Recommendation
Invalidate (or refresh) the market's `index-cache- {timestamp, aid}` entry whenever `socialize-debt` (or any other function that mutates `index`/`lindex` outside of `accrue`) is called, e.g., by having `socialize-debt` return the new indexes and having the market update `index-cache` accordingly, or by having the market re-derive indexes directly from the vault instead of trusting a same-block cache whenever a socialize-debt event has occurred for that asset in the block.

### Proof of Concept
1. Block N: Transaction A triggers `accrue-and-cache STX` in market, caching `{timestamp: T, aid: STX} -> {index: I, lindex: L}`.
2. Same block N: A liquidation/bad-debt event calls `.v0-vault-stx socialize-debt scaled-amount`, which computes `new-lindex < L` and writes it directly to the vault's `lindex` var [7](#0-6)  — the market's `index-cache` still holds stale `L`.
3. Same block N: A user borrows against zSTX collateral; market calls `accrue-and-cache STX` again, hits the cache, and returns stale `L` instead of `new-lindex`, overvaluing the zSTX collateral used in the health check.
4. The user's position passes the health check using inflated collateral value and successfully increases their debt, even though true collateral value (post-socialize-debt) would have failed the check.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L243-257)
```text
;; -- Accrual & caching ------------------------------------------------------

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
