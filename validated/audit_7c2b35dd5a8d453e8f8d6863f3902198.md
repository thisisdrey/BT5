### Title
Inconsistent rounding direction between actual-debt used in health/liquidatability reporting and the vault's own debt accounting - ([File: mainnet/contracts/utility/v0-1-data.clar])

### Summary
`v0-1-data.clar` recomputes a user's "actual debt" from `scaled` principal and the vault's `borrow-index` using `mul-div-down`, while every vault contract (`v0-vault-*.clar`) computes the authoritative debt figure used for caps, repay/interest accounting, and treasury minting via `calc-cumulative-debt`, which rounds **up** (`mul-div-up`). This mirrors the analog report's `applyInterest` inconsistency: the same conceptual quantity ("principal with interest applied") is computed with two different rounding conventions in two different call paths, producing two different numeric answers for the same account/asset/index.

### Finding Description
The vault's canonical debt figure is produced by: [1](#0-0) 
which calls `calc-cumulative-debt`, defined to round the result **up**: [2](#0-1) 

By contrast, `v0-1-data.clar`'s `build-debt-entry` and `sum-debt-usd` helpers - used to compute `actual-debt`, `interest-accrued`, `total-debt-usd`, `current-ltv`, and `health-factor`/`is-liquidatable` for `get-user-position` - independently recompute "actual debt" from the same `scaled` principal and `borrow-index`, but round **down**: [3](#0-2) [4](#0-3) 

These two independently-derived debt figures for the same account state will diverge by up to 1 unit per asset per call (rounding-down vs rounding-up on `mul-div`), and the divergence compounds because `current-ltv`, `health-factor`, and `is-liquidatable` in `get-user-position` all derive from the rounded-down `debt-usd`: [5](#0-4) 

### Impact Explanation
`get-user-position` is a read-only reporting function; I could not confirm from the indexed contents that `v0-4-market.clar`'s own enforcement path (borrow caps, liquidation eligibility checks) calls into `v0-1-data.clar` for its actual liquidation/health decision rather than computing its own health factor internally. Without that confirmation, the rounding-direction mismatch demonstrably produces an **inconsistent, incorrect reported debt/health-factor/LTV** relative to the vault's ground truth, but I cannot establish with the available index contents that this incorrect value is actually consumed by an enforcement path (e.g., that a liquidator or borrower could exploit the discrepancy to borrow past caps or evade/trigger liquidation) as opposed to being purely an off-chain/UI display bug. This uncertainty is a limitation of the codebase index, not a claim that no impact exists.

### Likelihood Explanation
The divergence occurs on every single call to `get-user-position` for any account with nonzero debt, since it's a deterministic result of two different rounding directions applied to the same formula - no attacker action is required to trigger the mismatch itself.

### Recommendation
Use a single shared debt-computation function (matching the vault's `calc-cumulative-debt` / `mul-div-up` rounding) in `v0-1-data.clar`'s `build-debt-entry` and `sum-debt-usd`, rather than reimplementing the "principal × index" calculation with a different rounding mode. If `v0-1-data.clar`'s health/LTV output is ever consumed by any enforcement logic (borrow, liquidation, or repay flows), that reliance should be reviewed and removed in favor of vault-native debt figures.

### Proof of Concept
1. A user borrows from `vault-stx`, producing `principal-scaled` = `S` and vault `index` = `I`.
2. The vault's own debt (used internally for caps/repay) is `calc-cumulative-debt(S, I) = ceil(S*I/PRECISION)`: [2](#0-1) 
3. `v0-1-data.clar`'s `get-user-position` independently computes the same figure as `floor(S*I/PRECISION)` via `build-debt-entry`/`sum-debt-usd`: [6](#0-5) [7](#0-6) 
4. For any `S*I` not evenly divisible by `PRECISION`, these two values differ by exactly 1 unit, so `total-debt-usd`, `current-ltv`, `health-factor`, and `is-liquidatable` reported to callers of `get-user-position` do not match the vault's authoritative debt state.

### Citations

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L331-332)
```text
(define-private (debt-preview)
  (calc-cumulative-debt (var-get principal-scaled) (next-index)))
```

**File:** local-testing/contracts/vault/vault-sbtc.clar (L181-182)
```text
(define-private (calc-cumulative-debt (principal-amount uint) (idx uint))
  (mul-div-up principal-amount idx INDEX-PRECISION))
```

**File:** mainnet/contracts/utility/v0-1-data.clar (L444-472)
```text
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

**File:** mainnet/contracts/utility/v0-1-data.clar (L489-507)
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
```

**File:** mainnet/contracts/utility/v0-1-data.clar (L526-535)
```text
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
