### Title
Single Broken Asset Oracle/Index Permanently Blocks Liquidation and Withdrawal of an Otherwise Healthy Position - (File: `local-testing/contracts/market/market.clar` / `mainnet/contracts/market/v0-4-market.clar`)

### Summary
`get-assets` resolves prices for every asset a position touches via `price-multi-resolve`, using `unwrap-panic` with no partial-success/bypass path. If price resolution for a single asset in a user's mask permanently fails (feed removed/never published, DIA/Pyth revert, or a ztoken's `resolve-ztoken`/`get-cached-indexes` failing because the underlying vault's `accrue` cannot succeed), the *entire* multi-asset resolution aborts, and every canonical operation that depends on it (`liquidate`, `collateral-remove` with debt, `borrow`, `collateral-add`) becomes permanently unusable for that position — with no fallback to resolve/skip only the healthy assets. This mirrors the Across report's "failed messenger renders canonical methods useless": one failing sub-call in a mandatory, non-bypassable sequential dependency chain freezes the whole higher-level operation, including the parts unrelated to the failure.

### Finding Description
`get-assets` builds the oracle/callcode tuple list for all assets in a user's `safe-mask` and calls: [1](#0-0) 

`price-multi-resolve` folds over every oracle entry and asserts `valid` must remain `true` for the *whole* list; a single failure anywhere flips `valid` to `false` and the read fails entirely, with no way to drop just the broken asset: [2](#0-1) 

Each entry resolution (`price-resolve`) itself has no fallback if the underlying call reverts: Pyth (`call-pyth`/`resolve-pyth`), DIA (`call-dia`/`resolve-dia`), or the ztoken callcode transform (`resolve-ztoken`, which requires `get-cached-indexes` to have been populated by a prior `accrue-and-cache`/`vault-accrue` call) can each fail and bubble the error: [3](#0-2) [4](#0-3) 

`liquidate` explicitly depends on this chain: it must accrue+cache every collateral/debt asset in the position, then call `get-assets(mask)` (which panics on any failure) before it can compute LTV, penalties, and proceed to `vault-system-repay`: [5](#0-4) 

`collateral-remove` (when the position has debt) and `collateral-add` (when adding new collateral to a position with existing debt) follow the identical pattern — `accrue-user-debts`/`accrue-user-collateral` then `get-assets(mask)` — so they are equally blocked: [6](#0-5) [7](#0-6) 

There is no admin/permissionless override to force-resolve only the healthy subset of assets or to bypass the failing one (unlike the "disabled collateral" path in `collateral-remove`, which only applies when the *removed* asset itself is not part of the mask, not when a *different* mask asset is the broken one). Once one asset a borrower is exposed to becomes unresolvable, the entire position is frozen for all mask-dependent canonical operations.

### Impact Explanation
This lands on **Critical – protocol insolvency / permanent freezing of funds**. A borrower whose position mask includes one asset with a permanently reverting oracle/index (e.g., a Pyth feed id that stops being published, a DIA lookup key issue, or a ztoken whose underlying vault accrual cannot complete) cannot be liquidated even while genuinely unhealthy, since `liquidate` requires `get-assets(mask)` to succeed for the whole mask. Bad debt accrues against the protocol with no liquidation path, and the same borrower's healthy collateral in other assets is frozen (cannot be withdrawn/adjusted) because `collateral-remove`/`collateral-add` share the same all-or-nothing resolution. This is functionally identical to the report's "no way to transfer funds until the messenger resolves" scenario, except the frozen resource here is a lending position and the resulting bad debt exposes the protocol to insolvency risk.

### Likelihood Explanation
Requires only a routine (non-malicious, non-DAO) fault: a Pyth feed becoming stale/unpublished for longer than allowed, a DIA lookup returning an error, or a vault accrual step failing for any of a position's collateral assets. No governance compromise or oracle data manipulation is needed — the flaw is architectural (fail if any one of N resolutions fails, not resolve what can be resolved), making the trigger condition realistic and outside the operator's control once it occurs.

### Recommendation
Do not require all-or-nothing resolution across a position's full asset mask for critical safety operations like `liquidate`. Consider: (1) allowing `liquidate` to proceed using only assets whose prices resolve successfully, explicitly excluding the borrower's debt/collateral in the broken asset from the notional calculation with a documented, safe fallback (e.g., treat unresolvable assets conservatively), or (2) providing an admin-gated (non-DAO, e.g., pausable/whitelisted keeper) emergency path to force-liquidate positions whose oracle failure is isolated to a single asset, so bad debt cannot silently accumulate while healthy collateral is simultaneously frozen.

### Proof of Concept
1. Borrower opens a position with collateral in `sBTC` (healthy Pyth feed) and additional collateral in `zUSDH` (a ztoken whose price requires `resolve-ztoken` → `get-cached-indexes`/`vault-accrue` on `.vault-usdh`).
2. The `USDH` oracle feed (DIA) or the `vault-usdh` accrual path begins reverting persistently (e.g., DIA lookup key becomes invalid, or the DIA contract itself is paused/broken) — an external, non-malicious dependency failure, not attacker-supplied bad data.
3. Borrower's `sBTC` debt grows past the liquidation threshold (via normal interest accrual/price movement in `sBTC`), making the position liquidatable purely on `sBTC` terms.
4. Any liquidator calls `liquidate`; execution reaches `get-assets(mask)` → `price-multi-resolve`, which attempts to resolve `USDH`'s price as part of the mask and panics/`ERR-ORACLE-MULTI`s, aborting the entire `liquidate` call before `sBTC` debt/collateral is ever processed. [8](#0-7) 
5. The position remains under-collateralized indefinitely; no liquidation path exists until the `USDH` oracle/vault issue is fixed, while the borrower's `sBTC` collateral is simultaneously frozen from withdrawal via `collateral-remove` for the same reason.

### Citations

**File:** local-testing/contracts/market/market.clar (L317-370)
```text
(define-private (call-pyth (ident (buff 32)))
  ;; @mainnet: (let ((res (unwrap! (contract-call? 'SP1CGXWEAMG6P6FT04W66NVGJ7PQWMDAC19R7PJ0Y.pyth-storage-v4 get-price ident) ERR-ORACLE-PYTH)))
  (let ((res (unwrap! (contract-call? .pyth-storage-v4 get-price ident) ERR-ORACLE-PYTH)))
    (ok res)))

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

;; @staging
;; Mock oracle for testing bad debt socialization
(define-private (call-mock (key (string-ascii 32)))
  (let ((res (unwrap! (contract-call? .mock-oracle get-value key) ERR-ORACLE-MOCK)))
    (ok res)))

(define-private (resolve-mock (ident (buff 32)))
  (let ((key (unwrap-panic (from-consensus-buff? (string-ascii 32) ident)))
        (res (try! (call-mock key))))
    (ok res)))

(define-private (resolve-price-feed (type (buff 1)) (ident (buff 32)))
  (if (is-eq type TYPE-PYTH) (resolve-pyth ident)
  (if (is-eq type TYPE-DIA) (resolve-dia ident)
  (if (is-eq type TYPE-MOCK) (resolve-mock ident)
  ERR-ORACLE-TYPE))))

;; -- Oracle: callcode transformations ---------------------------------------

(define-private (resolve-ststx (p uint))
  (let ((ratio (unwrap! (call-ststx-ratio) ERR-ORACLE-CALLCODE)))
    (ok (mul-div-down p ratio STSTX-RATIO-DECIMALS))))

(define-private (resolve-ztoken (p uint) (aid uint))
  (let ((cached (unwrap! (get-cached-indexes aid) ERR-ORACLE-CALLCODE))
        (cached-lindex (get lindex cached))
        (scaled (* p cached-lindex)))
    (ok (div-down scaled INDEX-PRECISION))))

```

**File:** local-testing/contracts/market/market.clar (L371-417)
```text
(define-private (resolve-callcode (p uint) (callcode (optional (buff 1))))
  (let ((cc (unwrap! callcode (ok p))))
    (if (is-eq cc CALLCODE-STSTX) (resolve-ststx p)
    (if (is-eq cc CALLCODE-ZSTX) (resolve-ztoken p STX)
    (if (is-eq cc CALLCODE-ZSBTC) (resolve-ztoken p sBTC)
    (if (is-eq cc CALLCODE-ZSTSTX) (resolve-ztoken (try! (resolve-ststx p)) stSTX)
    (if (is-eq cc CALLCODE-ZUSDC) (resolve-ztoken p USDC)
    (if (is-eq cc CALLCODE-ZUSDH) (resolve-ztoken p USDH)
    (if (is-eq cc CALLCODE-ZSTSTXBTC) (resolve-ztoken p stSTXbtc)
    ERR-ORACLE-CALLCODE)))))))))

;; -- Oracle: price resolution -----------------------------------------------

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

**File:** local-testing/contracts/market/market.clar (L419-440)
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

**File:** local-testing/contracts/market/market.clar (L1061-1076)
```text
          (if is-new-collateral
              (let ((position (try! (get-position account)))
                    (current-mask (get mask position))
                    (future-mask (bit-or current-mask (pow u2 asset-id)))
                    (future-group (try! (get-egroup future-mask)))
                    ;; Accrue positions (required for price resolution)
                    (u-debt (accrue-user-debts (get debt position)))
                    (u-coll (accrue-user-collateral (get collateral position)))

                    ;; Get current egroup and notional values
                    (current-group (try! (get-egroup current-mask)))
                    (current-ltv (buff-to-uint-be (get LTV-BORROW current-group)))
                    (feeds-check (try! (write-feeds price-feeds)))
                    (current-assets (get-assets current-mask))
                    (current-notional (get-notional-evaluation { position: position, assets: current-assets }))
                    (current-debt-usd (get debt current-notional)))
```

**File:** local-testing/contracts/market/market.clar (L1141-1159)
```text
    (if has-debt
        ;; HAS DEBT: Full flow with price resolution and health checks
        (let ((is-collateral-enabled (get collateral asset))
              (feeds-check (try! (write-feeds price-feeds)))
              (position-mask (get mask position))
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

          (asserts! (is-healthy collateral-value debt-value current-ltvb) ERR-UNHEALTHY)
```

**File:** local-testing/contracts/market/market.clar (L1405-1436)
```text
(define-public (liquidate
                (borrower principal)
                (collateral-ft <ft-trait>)
                (debt-ft <ft-trait>)
                (debt-amount uint)
                (min-collateral-expected uint)
                (collateral-receiver (optional principal))
                (price-feeds (optional (list 3 (buff 8192)))))
  (let (
    (feeds-check (try! (write-feeds price-feeds)))
    (liquidator contract-caller)
    (position (try! (get-liquidation-position borrower)))
    (pos-full (try! (get-full-position borrower)))
    (mask (get mask position))
    (group (try! (get-egroup mask)))

    (coll-address (contract-of collateral-ft))
    (debt-address (contract-of debt-ft))
    (coll-asset (try! (get-asset coll-address)))
    (debt-asset (try! (get-asset debt-address)))
    (coll-aid (get id coll-asset))
    (debt-aid (get id debt-asset))

    ;; accrue FIRST - populates cache for zToken price resolution
    (u-debt (accrue-user-debts (get debt pos-full)))
    (u-coll (accrue-user-collateral (get collateral pos-full)))

    ;; NOW safe to resolve prices (cache is populated)
    (assets (get-assets mask))
    (notional-valued-assets (get-notional-evaluation { position: position, assets: assets }))
    (total-collateral-usd (get collateral notional-valued-assets))
    (total-debt-usd (get debt notional-valued-assets))
```
