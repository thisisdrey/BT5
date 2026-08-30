Confirmed: `get-index` in the vault contracts returns the raw stored `(var-get index)` — the last on-chain accrued value — not `next-index`, which is the freshly time-accrued value. [1](#0-0) 
Meanwhile `next-index` computes the up-to-date index by applying the elapsed-time multiplier since `last-update`. [2](#0-1) 

### Title
`get-user-position` reports stale, non-accrued borrow index causing health/liquidatable status to understate risk - (File: `mainnet/contracts/utility/v0-1-data.clar`)

### Summary
`protocol-data`/`v0-1-data`'s `get-user-position` (and `get-user-borrows`) build a user's health snapshot using `get-vault-borrow-index`, a helper that calls the vault's `get-index` read-only function. `get-index` returns the raw stored index variable rather than the time-accrued `next-index`/`debt-preview` value, so any interest accrued since the vault's `last-update` timestamp is omitted from `actual-debt`, `total-debt-usd`, `current-ltv`, `health-factor`, and `is-liquidatable`.

### Finding Description
`build-debt-entry` and `sum-debt-usd` compute a user's actual debt as `scaled * borrow-index / INDEX-PRECISION`, where `borrow-index` comes from `get-vault-borrow-index`, itself simply forwarding to the vault's `get-index`: [3](#0-2) [4](#0-3) 

`get-index` is defined as `(ok (var-get index))` — the last value persisted on-chain the last time `accrue` was actually invoked (e.g. by a `borrow`/`repay`/`system-borrow` call), while the vault separately exposes `get-next-index` computed from elapsed time via `next-index`: [1](#0-0) [5](#0-4) 

If no one has interacted with a given vault recently (no `borrow`/`repay`/`system-borrow`/`system-repay` triggering `accrue`), `var-get index` can lag significantly behind the true, currently-owed debt. `get-user-position`'s `health-factor` and `is-liquidatable` fields are computed directly from this stale `debt-usd`: [6](#0-5) 

This mirrors the Notional M-2 pattern exactly: a view function meant to be polled by an off-chain component (here, liquidation bots/keepers/frontends checking whether a position is liquidatable) uses the last-persisted (stale) index instead of the fresh, time-accrued value that the actual on-chain `liquidate()`/`borrow()` functions use (those explicitly call `accrue-and-cache`/`vault-accrue` before evaluating health, per `market.clar`'s `liquidate` and `borrow` functions which call `accrue-user-debts`/`accrue-and-cache` prior to computing `notional-valued-assets`). [7](#0-6) [8](#0-7) 

### Impact Explanation
`get-user-position`/`get-user-borrows` are the canonical data-source functions liquidation keepers/bots and frontends are expected to poll to decide when to call `liquidate()`, exactly analogous to Notional's `checkRebalance()` being polled by the Gelato rebalancing bot. When a vault has gone a while without an accrual-triggering interaction, the stale index understates a borrower's true debt, understating `current-ltv` and `health-factor`, and can report `is-liquidatable: false` for a position that is actually eligible (or already unsafely) liquidatable on-chain. This delays the triggering of `liquidate()` by off-chain actors who rely on this view, allowing undercollateralized positions to persist and their bad debt/interest shortfall to grow, which is a temporary/delayed freezing-and-risk condition on protocol solvency until someone else interacts with the vault to force an accrual. This lands in the temporary freezing-of-funds impact category since liquidations that should occur are delayed, and collateral proceeds that would otherwise be recovered promptly for the protocol/liquidators are frozen until a chance on-chain interaction resynchronizes the index.

### Likelihood Explanation
Likelihood is moderate: it requires a vault to go without a `borrow`/`repay`/`system-borrow`/`system-repay` (accrual-triggering) call for a period while a borrower's position deteriorates, which is plausible for lower-activity vaults (e.g. exotic collateral pairs) or during periods of low usage. No privileged access or malicious action is required — it's a straightforward staleness gap between the read-only aggregator and the accrual-triggering write paths.

### Recommendation
Change `get-vault-borrow-index` (and the equivalent liquidity-index lookups used for zToken pricing) in `v0-1-data.clar`/`protocol-data.clar` to call the vault's `get-next-index` (and `next-liquidity-index`-equivalent) instead of `get-index`, so `get-user-position`/`get-user-borrows` always reflect the fully time-accrued debt/collateral values, consistent with what `liquidate()`/`borrow()` use internally.

### Proof of Concept
1. Borrower opens a position in a low-traffic vault (e.g. `v0-vault-ststxbtc`) with debt near the liquidation threshold.
2. No one calls `borrow`/`repay`/`system-borrow`/`system-repay` on that vault for an extended period, so `var-get index` (and `last-update`) remain frozen while `next-index` (time-accrued) continues to grow.
3. A liquidation bot polls `get-user-position(borrower)`; it computes `debt-usd` from the stale `get-vault-borrow-index`, understating `current-ltv` and returning `is-liquidatable: false`, even though the position's true, time-accrued LTV already exceeds `ltv-liq-partial`.
4. The bot skips calling `liquidate()`. Eventually anyone triggering `accrue` on that vault (e.g., another user's `borrow`) syncs the index and the position's true unhealthy state becomes visible/callable on-chain, but liquidation was delayed relative to when it should have occurred, during which the borrower's bad debt/interest shortfall against collateral value grows.

### Citations

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L470-478)
```text
(define-read-only (get-principal-scaled) (ok (var-get principal-scaled)))
(define-read-only (get-index) (ok (var-get index)))
(define-read-only (get-last-update) (ok (var-get last-update)))
(define-read-only (get-debt) (ok (total-debt)))
(define-read-only (get-utilization) (ok (utilization)))
(define-read-only (get-interest-rate) (ok (interest-rate)))
(define-read-only (get-next-index) (ok (next-index)))
(define-read-only (get-principal-ratio-reduction (amount uint)) (ok (principal-ratio-reduction amount)))
(define-read-only (get-liquidity-index) (ok (var-get lindex)))
```

**File:** local-testing/contracts/vault/vault-sbtc.clar (L381-392)
```text
(define-private (next-index)
  (let ((states (var-get pause-states))
        (idx (var-get index)))
    (if (get accrue states)
        idx
        (let (
            (rate (interest-rate))
            (time-delta (- stacks-block-time (var-get last-update)))
            (multiplier (if (is-eq time-delta u0)
                          INDEX-PRECISION
                          (calc-multiplier-delta rate time-delta true))))
          (calc-index-next idx multiplier)))))
```

**File:** mainnet/contracts/utility/v0-1-data.clar (L179-186)
```text
(define-private (get-vault-borrow-index (vid uint))
  (if (is-eq vid STX) (unwrap-panic (contract-call? .v0-vault-stx get-index))
  (if (is-eq vid sBTC) (unwrap-panic (contract-call? .v0-vault-sbtc get-index))
  (if (is-eq vid stSTX) (unwrap-panic (contract-call? .v0-vault-ststx get-index))
  (if (is-eq vid USDC) (unwrap-panic (contract-call? .v0-vault-usdc get-index))
  (if (is-eq vid USDH) (unwrap-panic (contract-call? .v0-vault-usdh get-index))
  (if (is-eq vid stSTXbtc) (unwrap-panic (contract-call? .v0-vault-ststxbtc get-index))
  INDEX-PRECISION)))))))
```

**File:** mainnet/contracts/utility/v0-1-data.clar (L441-472)
```text
              ;; Map each debt entry to enriched format with actual balances
              (enriched-debts (map build-debt-entry debt-list))
              ;; Calculate notional values
              (coll-usd (fold sum-collateral-usd collateral-list u0))
              (debt-usd (fold sum-debt-usd debt-list u0))
              ;; Calculate LTV
              (current-ltv (if (is-eq coll-usd u0)
                              (if (is-eq debt-usd u0) u0 BPS)
                              (mul-div-down debt-usd BPS coll-usd)))
              ;; Get egroup for health calculation
              (egroup-result (contract-call? .v0-egroup resolve mask)))
          (match egroup-result
            egroup
              (let ((ltv-borrow (buff-to-uint-be (get LTV-BORROW egroup)))
                    (ltv-liq-partial (buff-to-uint-be (get LTV-LIQ-PARTIAL egroup)))
                    ;; Health factor: (coll x ltv-borrow) / debt, scaled to BPS
                    ;; >10000 = healthy, <10000 = unhealthy
                    (health-factor (if (is-eq debt-usd u0)
                                      u100000000  ;; Infinite health if no debt
                                      (mul-div-down (mul-bps-down coll-usd ltv-borrow) BPS debt-usd))))
                (ok {
                  account: account,
                  mask: mask,
                  collateral: collateral-list,
                  debt: enriched-debts,
                  total-collateral-usd: coll-usd,
                  total-debt-usd: debt-usd,
                  current-ltv: current-ltv,
                  ltv-borrow: ltv-borrow,
                  ltv-liq-partial: ltv-liq-partial,
                  health-factor: health-factor,
                  is-liquidatable: (>= current-ltv ltv-liq-partial)
```

**File:** mainnet/contracts/utility/v0-1-data.clar (L489-535)
```text
;; Helper: Build enriched debt entry with actual balance
(define-private (build-debt-entry (debt-entry { aid: uint, scaled: uint }))
  (let ((aid (get aid debt-entry))
        (scaled (get scaled debt-entry))
        (asset-status (unwrap-panic (contract-call? .v0-assets get-status aid)))
        (borrow-index (get-vault-borrow-index aid))
        ;; Calculate actual debt with compound interest
        (actual (mul-div-down scaled borrow-index INDEX-PRECISION))
        ;; Interest accrued = actual - scaled (simplified, assumes initial index ~= PRECISION)
        (interest (if (> actual scaled) (- actual scaled) u0)))
    {
      asset-id: aid,
      asset-addr: (get addr asset-status),
      underlying: (get addr asset-status),
      scaled-debt: scaled,
      borrow-index: borrow-index,
      actual-debt: actual,
      interest-accrued: interest
    }))

;; Helper: Sum collateral USD values
(define-private (sum-collateral-usd (entry { aid: uint, amount: uint }) (acc uint))
  (let ((aid (get aid entry))
        (amount (get amount entry))
        (asset-data (unwrap-panic (contract-call? .v0-assets get-status aid)))
        (decimals (get decimals asset-data))
        (price (get-asset-price aid)))
    (+ acc (/ (* amount price) (pow u10 decimals)))))

;; Helper: Find specific asset amount in collateral list
(define-private (find-collateral-amount-iter
  (entry { aid: uint, amount: uint })
  (acc { target: uint, amount: uint }))
  (if (is-eq (get aid entry) (get target acc))
      { target: (get target acc), amount: (get amount entry) }
      acc))

;; Helper: Sum debt USD values
(define-private (sum-debt-usd (entry { aid: uint, scaled: uint }) (acc uint))
  (let ((aid (get aid entry))
        (scaled (get scaled entry))
        (asset-data (unwrap-panic (contract-call? .v0-assets get-status aid)))
        (decimals (get decimals asset-data))
        (borrow-index (get-vault-borrow-index aid))
        (actual (mul-div-down scaled borrow-index INDEX-PRECISION))
        (price (get-asset-price aid)))
    (+ acc (/ (* actual price) (pow u10 decimals)))))
```

**File:** local-testing/contracts/vault/vault-stx.clar (L379-392)
```text
(define-private (next-index)
  (let ((states (var-get pause-states))
        (idx (var-get index)))
    (if (get accrue states)
        idx
        (let (
            (rate (interest-rate))
            (time-delta (- stacks-block-time (var-get last-update)))
            (multiplier (if (is-eq time-delta u0)
                          INDEX-PRECISION
                          (calc-multiplier-delta rate time-delta true))))
          (calc-index-next idx multiplier)))))

(define-private (next-liquidity-index)
```

**File:** local-testing/contracts/market/market.clar (L1273-1290)
```text
        ;; Step 2: Accrue user's positions (populates cache for ztokens)
        (u-debt (accrue-user-debts (get debt position)))
        (u-coll (accrue-user-collateral (get collateral position)))
        
        ;; Step 3: Accrue the asset being borrowed (needed for index access)
        (unused (accrue-and-cache asset-id))
        
        ;; Step 4: NOW safe to resolve prices (cache is populated)
        (assets (get-assets mask))

        ;; Calculate current health with current mask
        (current-group (try! (get-egroup mask)))
        (current-ltvb (buff-to-uint-be (get LTV-BORROW current-group)))

        ;; LTV
        (notional-valued-assets (get-notional-evaluation { position: position, assets: assets }))
        (collateral-value (get collateral notional-valued-assets))
        (debt-value (get debt notional-valued-assets)))
```

**File:** local-testing/contracts/market/market.clar (L1428-1435)
```text
    ;; accrue FIRST - populates cache for zToken price resolution
    (u-debt (accrue-user-debts (get debt pos-full)))
    (u-coll (accrue-user-collateral (get collateral pos-full)))

    ;; NOW safe to resolve prices (cache is populated)
    (assets (get-assets mask))
    (notional-valued-assets (get-notional-evaluation { position: position, assets: assets }))
    (total-collateral-usd (get collateral notional-valued-assets))
```
