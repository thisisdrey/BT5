# Analysis Result

## Title
Silent Fallback to Zero on Asset Lookup Miss in Notional Value Resolution - (File: `mainnet/contracts/market/v0-4-market.clar`)

### Summary
`find-and-resolve-asset-value` — a helper used in market health-check/notional valuation paths — silently returns `u0` when the requested asset id is not present in the `assets` list it is given, instead of reverting or signaling an error. This mirrors the CVE-2022-39389 bug class: a parsing/lookup operation that can fail on an edge case but, instead of surfacing that failure, silently falls through to a default value while the rest of the system keeps operating normally on the (now incorrect) state.

### Finding Description
`find-and-resolve-asset-value` looks up an asset entry by id in a caller-supplied list via `find-asset`, and on a lookup miss (`match ... u0` fallback) returns `0` for that asset's USD value rather than aborting: [1](#0-0) 

This is inconsistent with sibling helpers in the same file that treat a missing-asset lookup as a hard failure. `process-debt-asset` uses `unwrap-panic` on the equivalent `find-asset` call (aborts the transaction on miss): [2](#0-1) 

while `process-collateral-asset` treats a miss as an explicit, deliberately-handled "disabled collateral" case with an on-demand price resolution (an intentional design decision, out of scope per the rules): [3](#0-2) 

`find-and-resolve-asset-value`, by contrast, has no such explicit handling — a miss just becomes `0` with no distinguishing signal from a legitimately zero-valued asset. Any accounting path that calls this helper (it has multiple call sites in `market.clar`/`v0-4-market.clar`) for a debt or collateral asset that is absent from the `assets` list passed in — e.g., due to a mismatch between the enabled-asset bitmap used to build that list and the actual assets held/owed in a user's position — will silently under-count that asset's USD value rather than reverting.

### Impact Explanation
If a debt or collateral asset's value is silently treated as `0` instead of triggering a revert, a position's computed collateral/debt notionals used in `is-healthy` / `is-healthy-with-mask` health checks can be wrong in the direction that hides real debt or inflates apparent safety: [4](#0-3) 

An under-counted debt value can let an unhealthy position pass a borrow/withdraw health check, and an under-counted collateral value can cause legitimate positions to be misclassified as unhealthy/undercollateralized. Either outcome is a silent accounting corruption analogous to lnd's silent "degraded state" (undetected on-chain events while the node keeps operating normally) — the market keeps processing transactions normally while its risk math is quietly wrong, which can lead to protocol insolvency or temporary freezing of funds for affected positions.

### Likelihood Explanation
The severity depends entirely on whether any of the ~6 call sites of `find-and-resolve-asset-value` can be reached with an `assets` list that omits an asset that is actually part of the position being evaluated (e.g., a timing/ordering mismatch between the enabled-asset bitmap snapshot used to build `assets` via `get-assets`/`get-status-multi` and the assets actually recorded in the user's position). I was not able to fully trace every call site of this helper within the available tool budget, so I cannot confirm with certainty that such a mismatch is reachable purely from an ordinary principal's call without additional conditions (e.g., DAO asset-registry changes, which are out of scope). This is a real code-level inconsistency (silent-zero vs. hard-revert on the same class of lookup miss) worth an explicit audit of all `find-and-resolve-asset-value` call sites to confirm reachability.

### Recommendation
Make `find-and-resolve-asset-value` fail closed like `process-debt-asset` does: replace the `u0` fallback with an explicit error (`unwrap!`/`asserts!`) so a missing asset in the `assets` list aborts the transaction instead of silently zeroing its value, and audit all call sites to ensure the `assets` list passed in is guaranteed to be a superset of the position's actual collateral/debt asset ids.

### Proof of Concept
Not independently reproducible from indexed context alone — this requires tracing all call sites of `find-and-resolve-asset-value` in `market.clar` (`mainnet/contracts/market/v0-4-market.clar`, and mirrored in `local-testing/contracts/market/market.clar`) to construct a concrete transaction where the `assets` argument omits an asset actually held/owed by the position under evaluation. A Devin session with full repo/tool access should grep all 6 usages, confirm the `assets` list construction (`get-assets`) versus the position's `collateral`/`debt` lists always match by id, and write a Clarinet test exercising a borrow/withdraw where an enabled-bitmap/position mismatch is engineered (if reachable) to confirm the health check silently passes with an undercounted debt/collateral value. [5](#0-4)

### Citations

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

**File:** mainnet/contracts/market/v0-4-market.clar (L656-666)
```text
(define-private (is-healthy (collateral-usd uint) (debt-usd uint) (ltv uint))
  (if (is-eq debt-usd u0)
      true
      (<= (* debt-usd BPS) (* collateral-usd ltv))))

;; Check health using a custom mask's egroup rules
;; Returns true if position is healthy under the specified mask's LTV requirements
(define-private (is-healthy-with-mask (collateral-usd uint) (debt-usd uint) (mask uint))
  (let ((group (try! (get-egroup mask)))
        (ltvb (buff-to-uint-be (get LTV-BORROW group))))
    (ok (is-healthy collateral-usd debt-usd ltvb))))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L668-676)
```text
(define-private (find-and-resolve-asset-value
                  (assets (list 64 
                    { id: uint, addr: principal, decimals: uint,
                    oracle: { type: (buff 1), ident: (buff 32), callcode: (optional (buff 1)), max-staleness: uint },
                    collateral: bool, debt: bool, price: uint }))
                  (asset-id uint) (amount uint) (round-up bool))
  (match (find-asset asset-id assets)
    asset (normalize (* amount (get price asset)) (get decimals asset) round-up)
    u0))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L758-783)
```text
;; Process debt asset for liquidation
;; Finds asset info, converts to USD, caps at max liquidatable, converts back to token amount
;; Returns: { debt-actual-usd: uint, debt-actual: uint, debt-price: uint, debt-decimals: uint }
(define-private (process-debt-asset
  (debt-amount uint)
  (debt-aid uint)
  (max-debt-usd uint)
  (assets (list 64 {
    id: uint, addr: principal, decimals: uint,
    oracle: { type: (buff 1), ident: (buff 32), callcode: (optional (buff 1)), max-staleness: uint },
    collateral: bool, debt: bool, price: uint
  })))
  (let ((debt-asset-info (unwrap-panic (find-asset debt-aid assets)))
        (debt-price (get price debt-asset-info))
        (debt-decimals (get decimals debt-asset-info))
        (debt-usd (normalize (* debt-amount debt-price) debt-decimals false))
        ;; cap debt at maximum liquidatable amount
        (debt-actual-usd (if (> debt-usd max-debt-usd) max-debt-usd debt-usd))
        ;; convert capped USD amount back to token amount
        (debt-actual (mul-div-down debt-actual-usd (pow u10 debt-decimals) debt-price)))
    {
      debt-actual-usd: debt-actual-usd,
      debt-actual: debt-actual,
      debt-price: debt-price,
      debt-decimals: debt-decimals
    }))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L805-815)
```text
  (let (;; Calculate expected collateral in USD (with penalty bonus for liquidator)
        (coll-usd-expected (calc-liq-collateral-repay debt-actual-usd liq-penalty))
        
        ;; Handle disabled collaterals by resolving price if not in enabled assets
        (coll-asset-info (match (find-asset coll-aid assets)
                           ;; Found in enabled list: use it (already has price)
                           found found
                           ;; Not found (disabled): resolve price on demand
                           (let ((oracle-data (get oracle coll-asset))
                                 (price (unwrap-panic (price-resolve oracle-data))))
                             (merge coll-asset { price: price }))))
```
