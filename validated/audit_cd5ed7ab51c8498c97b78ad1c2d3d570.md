### Title
Liquidation curve exponent is silently ignored/approximated in `calc-liq-factor-exp`, causing incorrect liquidation penalty and collateral seizure - (File: `mainnet/contracts/market/v0-4-market.clar`)

### Summary
`calc-liq-factor-exp` is meant to raise the linear liquidation factor to a DAO-configured curve exponent (`LIQ-CURVE-EXP`) to produce a graduated liquidation curve. Instead of computing the actual configured exponent, the implementation falls back to a hardcoded, inaccurate approximation for any exponent that isn't exactly `BPS` (1.0) or an exact integer multiple of `BPS`, mirroring the class of bug in the referenced report: a simplified/approximated formula stands in for a more precise one, and the imprecise result feeds directly into a penalty/seizure calculation used against user funds.

### Finding Description
`calc-liq-factor-exp` is defined as: [1](#0-0) 

```clarity
(define-private (calc-liq-factor-exp (factor uint) (exp uint))
  (if (is-eq exp BPS) 
    factor
    (if (> exp BPS) 
        (/ (pow factor (/ exp BPS)) (pow BPS (- (/ exp BPS) u1)))
        (sqrti (* factor BPS))))) ;; assume factor^0.5
```

Two separate inaccuracies exist:
1. For any `exp` strictly between `0` and `BPS` (i.e. any configured curve exponent between 0 and 1, exclusive of 1), the code unconditionally computes `sqrti(factor * BPS)` — i.e. it always applies an exponent of exactly `0.5`, regardless of what the actual configured `LIQ-CURVE-EXP` value is (e.g. 0.3 or 0.8). The comment itself admits `;; assume factor^0.5`.
2. For `exp > BPS`, the code computes `(/ exp BPS)` using integer (floor) division. Any fractional multiple of `BPS` (e.g. `1.5 * BPS`) truncates to `1`, so `pow factor 1 / pow BPS 0 = factor` — collapsing the intended super-linear curve back down to a purely linear one.

This function's output, `liq-pct-scaled`, is used by `calc-liquidation-params` to derive both the liquidation penalty and the maximum liquidatable debt: [2](#0-1) 

which is invoked directly from the unprivileged, permissionless `liquidate` entry point available to any principal: [3](#0-2) 

The resulting `liq-pct-scaled` and `liq-penalty` values flow into `process-debt-asset`/`process-collateral-asset`/`calc-final-liquidation-amounts` to determine exactly how much debt is repaid and how much collateral is seized from the borrower: [4](#0-3) [5](#0-4) 

Because the exponent used in the actual seizure math does not match the exponent the DAO configured for the egroup (`LIQ-CURVE-EXP`, read via `get-egroup`), any liquidation on an egroup whose configured curve exponent is not exactly `BPS` or an exact integer multiple of `BPS` produces a liquidation percentage/penalty that diverges from the intended graduated curve — analogous to the original report where an inaccurate reward-rate approximation propagated into a slashing calculation, causing under/over-penalization.

### Impact Explanation
Since `liq-pct-scaled`/`liq-penalty` directly determine `max-debt-usd`, `coll-usd-expected`, and ultimately `coll-final`/`debt-to-repay` in `liquidate`, an incorrect exponent computation causes either:
- Excess collateral seized from a borrower relative to the correctly intended graduated-curve penalty (direct loss of user funds at rest for that borrower), or
- Insufficient debt repaid/penalty applied relative to intended design, degrading the intended incentive/safety curve and increasing exposure to bad-debt socialization for the pool (temporary freezing/loss of funds for the vault's other depositors via `socialize-debt`-adjacent bad-debt handling seen later in the same function).

This lands in the in-scope impact class: theft of user funds at rest (excess collateral seized beyond correctly configured curve) and/or protocol insolvency risk via mispriced liquidation penalties feeding into bad-debt socialization.

### Likelihood Explanation
Likelihood is tied purely to normal DAO configuration of `LIQ-CURVE-EXP` for an egroup (a legitimate, intended value, not a misconfiguration or compromise) combined with any liquidator calling the permissionless `liquidate` function once a position crosses into the partial-liquidation LTV band — both of these are routine, expected operations, not privileged or adversarial actions. Every liquidation on an egroup configured with a non-trivial (non-`BPS`, non-integer-multiple) curve exponent is affected, making this systematically reachable rather than an edge case.

### Recommendation
Implement `calc-liq-factor-exp` using an exponentiation method that supports fractional exponents scaled by `BPS` precision (e.g. a fixed-point `pow`/`exp`-`ln` based power function, or a lookup/interpolation table calibrated against the exact configured exponent), rather than special-casing only `exp == BPS` and approximating all other values as `0.5` or truncating fractional multiples of `BPS` via integer division.

### Proof of Concept
1. DAO/governance configures an egroup with `LIQ-CURVE-EXP` = `7000` (intended exponent 0.7) via the normal egroup-creation proposal flow (`v0-egroup.clar` / `proposal-create-multiple-egroups.clar`).
2. A borrower's position on that egroup crosses into the partial liquidation band (`current-ltv` between `ltv-liq-partial` and `ltv-liq-full`).
3. Any user calls `liquidate` on that position.
4. Inside `calc-liquidation-params` -> `calc-liq-factor-exp`, since `7000 < BPS (10000)`, the code computes `sqrti(factor * BPS)` (exponent 0.5) instead of `factor^0.7`, producing a `liq-pct-scaled` different from the DAO's intended curve.
5. This wrong `liq-pct-scaled` propagates into `liq-penalty` and `max-debt-usd`, changing the amount of collateral seized (`coll-final`) from the value the DAO's configured curve intended, directly affecting borrower collateral loss for every liquidation on that egroup.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L706-713)
```text
;; Apply curve exponent for graduated liquidation
;; liq-factor = liq-factor^alpha
(define-private (calc-liq-factor-exp (factor uint) (exp uint))
  (if (is-eq exp BPS) 
    factor
    (if (> exp BPS) 
        (/ (pow factor (/ exp BPS)) (pow BPS (- (/ exp BPS) u1)))
        (sqrti (* factor BPS))))) ;; assume factor^0.5
```

**File:** mainnet/contracts/market/v0-4-market.clar (L736-756)
```text
;; Graduated liquidation parameter calculation
;; Combines the 4-step liquidation factor calculation into a single helper
;; Returns: { liq-pct-scaled: uint, liq-penalty: uint, max-debt-usd: uint }
(define-private (calc-liquidation-params
  (current-ltv uint)
  (ltv-liq-partial uint)
  (ltv-liq-full uint)
  (liq-penalty-min uint)
  (liq-penalty-max uint)
  (curve-exponent uint)
  (total-debt-usd uint))
  
  (let ((liq-pct-linear (calc-liq-factor current-ltv ltv-liq-partial ltv-liq-full))
        (liq-pct-scaled (calc-liq-factor-exp liq-pct-linear curve-exponent))
        (liq-penalty (calc-liq-factor-bound liq-pct-scaled liq-penalty-min liq-penalty-max))
        (max-debt-usd (calc-liq-debt-repay total-debt-usd liq-pct-scaled)))
    {
      liq-pct-scaled: liq-pct-scaled,
      liq-penalty: liq-penalty,
      max-debt-usd: max-debt-usd
    }))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L758-784)
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

**File:** mainnet/contracts/market/v0-4-market.clar (L785-829)
```text
;; Process collateral asset for liquidation
;; Handles both enabled and disabled collateral assets
;; Calculates expected collateral, caps at user balance
;; Returns: { coll-actual: uint, coll-expected: uint, coll-price: uint, coll-decimals: uint }
(define-private (process-collateral-asset
  (coll-aid uint)
  (debt-actual-usd uint)
  (liq-penalty uint)
  (user-coll-balance uint)
  (assets (list 64 {
    id: uint, addr: principal, decimals: uint,
    oracle: { type: (buff 1), ident: (buff 32), callcode: (optional (buff 1)), max-staleness: uint },
    collateral: bool, debt: bool, price: uint
  }))
  (coll-asset {
    id: uint, addr: principal, decimals: uint,
    oracle: { type: (buff 1), ident: (buff 32), callcode: (optional (buff 1)), max-staleness: uint },
    collateral: bool, debt: bool
  }))
  
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
        (coll-price (get price coll-asset-info))
        (coll-decimals (get decimals coll-asset-info))
        (coll-expected (mul-div-down coll-usd-expected (pow u10 coll-decimals) coll-price))
        
        ;; cap at available collateral (user may not have enough)
        (coll-actual (if (> coll-expected user-coll-balance)
                         user-coll-balance
                         coll-expected)))
    {
      coll-actual: coll-actual,
      coll-expected: coll-expected,
      coll-price: coll-price,
      coll-decimals: coll-decimals
    }))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1437-1444)
```text
    ;; liquidation parameters (graduated liquidation calculation)
    (liq-params (calc-liquidation-params 
                  current-ltv ltv-liq-partial ltv-liq-full
                  liq-penalty-min liq-penalty-max 
                  curve-exponent total-debt-usd))
    (liq-pct-scaled (get liq-pct-scaled liq-params))
    (liq-penalty (get liq-penalty liq-params))
    (max-debt-usd (get max-debt-usd liq-params))
```
