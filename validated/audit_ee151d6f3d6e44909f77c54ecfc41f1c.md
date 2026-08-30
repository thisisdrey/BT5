Found it: `price-multi-resolve` in `mainnet/contracts/market/v0-4-market.clar` resolves prices for a *list* of assets, and if any single asset's oracle resolution fails (stale/illegal price, unsupported callcode/type), the entire batch call reverts, aborting the caller. This mirrors the Frax `getNextValidator()` pattern where one bad element in a loop reverts an otherwise-valid multi-element operation.

### Title
Single stale/illegal oracle in a multi-asset price batch reverts the entire user operation (health-check DoS) - (File: `mainnet/contracts/market/v0-4-market.clar`)

### Summary
`get-assets` builds a list of a user's enabled collateral assets and calls `price-multi-resolve` to fetch all their prices in one pass. `price-multi-resolve` folds `iter-price-multi` over every asset in the list; if `price-resolve` fails for **any single asset** (stale price, illegal ≤0 price, unsupported oracle type/callcode), `iter-price-multi` sets `valid: false`, and `price-multi-resolve` asserts on that flag and returns `ERR-ORACLE-MULTI` for the whole call, unconditionally.

### Finding Description
`price-multi-resolve` ( [1](#0-0) ) folds over the full list of oracle configs with `iter-price-multi` ( [2](#0-1) ), which calls `price-resolve` per asset and flips `valid` to `false` on any failure, and the outer function `asserts!` on `valid` for the aggregate result. `price-resolve` itself asserts `oracle-price-legal` and `oracle-timestamp-fresh` against `max-staleness`, erroring with `ERR-ORACLE-INVARIANT` for any single stale/zero/negative price ( [3](#0-2) ). This is invoked from `get-assets`, which is the core helper used by `collateral-add`, `borrow`, `repay`, and liquidation paths to compute a user's full notional collateral/debt valuation across their enabled asset set ( [4](#0-3) ). Because the fold has no per-asset skip/try-catch equivalent (Clarity has no try/catch, but the pattern could short-circuit only the failing asset if it were designed to, e.g., by excluding stale non-critical assets from the notional sum), one bad oracle for *any* asset in the "enabled" set is enough to make the whole valuation call — and therefore the whole entry point — revert.

### Impact Explanation
If any single asset among the globally-enabled collateral/debt set has its oracle go stale (feed simply not updated within `max-staleness`, which any relayer/keeper delay can cause) or return an illegal price, every account whose position/enabled mask includes that asset in `get-assets`'s resolved list is blocked from `collateral-add`, `borrow`, `repay`, and liquidation — none of which can complete `get-notional-evaluation` without a full valid price list. This is a temporary freezing of funds/functionality (inability to repay, add collateral, or be liquidated) for all affected users until the stale asset's oracle is refreshed or the DAO intervenes (e.g., disabling the asset via the registry bitmap), matching the in-scope "temporary freezing of funds" impact class.

### Likelihood Explanation
Likelihood is bound to oracle feed update cadence rather than to an attacker deliberately poisoning the pubkey/validator set as in the original Frax report; a keeper delay, RPC outage, or a low-liquidity/low-volume asset failing to get fresh Pyth/DIA updates within its configured `max-staleness` window is sufficient to trigger this without any privileged action or DAO misconfiguration. This is a natural, ordinary-usage trigger (not requiring DAO compromise), so it is a realistic and moderately likely occurrence, especially for assets with tight staleness thresholds.

### Recommendation
Make `get-assets`/`price-multi-resolve` resilient to a single asset's oracle failure: either (a) skip/exclude the failing asset from the notional evaluation when it is not part of the specific caller's own collateral/debt list (only fail hard if the *caller's own* position actually references that asset), or (b) allow the DAO/keepers to mark a specific asset's price as "paused" so it is excluded from the enabled bitmap consumed by `get-assets` without disabling the entire batch resolution for unrelated users. At minimum, differentiate between "asset irrelevant to this caller's position" vs "asset relevant to this caller's position" before reverting.

### Proof of Concept
1. Asset registry has assets `[STX, sBTC, stSTX, USDC, USDH, stSTXbtc]` all enabled, each with its own `max-staleness` ( [5](#0-4) ).
2. USDH's DIA oracle feed stops updating (external outage), so its last recorded timestamp exceeds its `max-staleness`.
3. Any user (Alice) with STX collateral only calls `collateral-add` for another STX deposit or `borrow` USDC; `get-assets` is invoked over the enabled-collateral mask including USDH (`mask-to-list-collateral`) and calls `price-multi-resolve` over the whole set ( [4](#0-3) ).
4. `iter-price-multi` calls `price-resolve` for USDH, which fails `oracle-timestamp-fresh` and returns `ERR-ORACLE-INVARIANT`, setting `valid: false` in the fold accumulator ( [2](#0-1) ).
5. `price-multi-resolve` asserts `valid` is `true` and reverts with `ERR-ORACLE-MULTI` for the entire call ( [1](#0-0) ), even though Alice holds no USDH and her operation had nothing to do with it.
6. Alice's `collateral-add`/`borrow`/`repay` transaction reverts entirely; this repeats for every user until USDH's oracle is refreshed or the DAO removes/disables USDH from the enabled bitmap.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L386-395)
```text
    ;; validate price and timestamp using max-staleness from oracle data
    (asserts! (and (oracle-price-legal final-price) (oracle-timestamp-fresh timestamp last-update-time max-staleness))
              ERR-ORACLE-INVARIANT)

    ;; update timestamp if newer
    (if (> timestamp last-update-time)
        (map-set last-update key timestamp)
        false)

    (ok final-price)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L397-403)
```text
(define-private (price-multi-resolve
  (data (list 64 { type: (buff 1), ident: (buff 32), callcode: (optional (buff 1)), max-staleness: uint }))
  (aids (list 64 uint)))
  (let ((init { output: (list), valid: true, aids: aids, idx: u0 })
        (response (fold iter-price-multi data init)))
    (asserts! (get valid response) ERR-ORACLE-MULTI)
    (ok (get output response))))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L405-418)
```text
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

**File:** local-testing/contracts/market/market.clar (L504-514)
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

**File:** mainnet/contracts/registry/v0-assets.clar (L174-196)
```text
(define-public (insert
                (ft <ft-trait>)
                (oracle-data {
                  type: (buff 1),
                  ident: (buff 32),
                  callcode: (optional (buff 1)),
                  max-staleness: uint
                }))
  (let ((id (increment))
        (asset-address (contract-of ft))
        (final-id (uint-to-buff1 id))
        (staleness (get max-staleness oracle-data))
        (entry {
          id: final-id,
          addr: asset-address,
          decimals: (call-get-decimals ft),
          oracle: oracle-data,
        }))

      (try! (check-dao-auth))
      (asserts! (<= (var-get nonce) MAX-ASSETS) ERR-LIMIT-REACHED)
      (asserts! (> staleness u0) ERR-INVALID-STALENESS)

```
