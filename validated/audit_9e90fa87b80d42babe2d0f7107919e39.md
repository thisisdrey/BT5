### Title
Integer-truncation and hard-coded exponent assumption break the graduated liquidation-penalty curve - (File: `mainnet/contracts/market/v0-4-market.clar`)

### Summary
`calc-liq-factor-exp` is documented and intended to raise the linear liquidation factor to a curve exponent configured per-egroup (`LIQ-CURVE-EXP`), but the implementation only behaves correctly for a narrow, undocumented subset of configured exponent values. Any `LIQ-CURVE-EXP` that is not an exact multiple of `BPS` (10000) — a value that is fully within the documented valid range and can be set by the DAO through ordinary egroup configuration — silently collapses the intended non-linear curve into either a flat linear factor or a fixed square-root approximation, exactly the kind of "assumption that does not hold" pattern flagged in the report.

### Finding Description
The relevant helper functions are: [1](#0-0) 

```clarity
;; Apply curve exponent for graduated liquidation
;; liq-factor = liq-factor^alpha
(define-private (calc-liq-factor-exp (factor uint) (exp uint))
  (if (is-eq exp BPS) 
    factor
    (if (> exp BPS) 
        (/ (pow factor (/ exp BPS)) (pow BPS (- (/ exp BPS) u1)))
        (sqrti (* factor BPS))))) ;; assume factor^0.5
```

Two separate invalid assumptions are baked into this code, mirroring the report's "invalid assumption" bug class:

1. **Aggressive-curve branch (`exp > BPS`)** uses `(/ exp BPS)` — integer division that truncates. Any `exp` value that is not an exact multiple of `BPS` (10000) collapses to the same integer as the nearest lower multiple. For example `exp = 15000` (documented as a valid "aggressive curve" value per `docs/egroups.md`, `LIQ-CURVE-EXP` range 0–10000+ bps) yields `(/ 15000 10000) = 1`, producing `(pow factor 1) / (pow BPS 0) = factor` — i.e. the exact same *linear* result as `exp = 10000`, silently disabling the intended steeper curve.
2. **Gentle-curve branch (`exp < BPS`)** hard-codes `sqrti (* factor BPS)`, which is only mathematically equal to `factor^0.5` — the code comment literally states `;; assume factor^0.5`. Any other sub-linear exponent configured by governance (e.g. 3000 or 7000 bps, both valid per the documented range `<10000: Gentle curve (e.g., square root)`) is silently treated as if it were exactly 5000 bps (0.5), producing a liquidation factor that does not match the configured curve at all.

This `liq-pct-scaled` value flows directly into `calc-liq-factor-bound` (penalty interpolation) and `calc-liq-debt-repay`/`max-debt-usd` (how much debt is liquidatable), both of which gate real value transfer during `liquidate`: [2](#0-1) 

Because these functions are invoked on every liquidation call from an ordinary, unprivileged liquidator against any borrower position, the miscomputed curve is reachable without any privileged action — the only precondition is that the affiliated egroup was configured (by the DAO, through normal, in-range parameter setting) with a `LIQ-CURVE-EXP` that is not an exact multiple of `10000`.

### Impact Explanation
Depending on the configured exponent, the bug can cause the liquidation percentage/penalty to be computed far lower or higher than the risk parameters intend:
- Under-computed `liq-pct-scaled` for undercollateralized positions caps the liquidatable debt below what governance intended, delaying/limiting liquidation of bad debt and increasing protocol insolvency risk on that egroup.
- Over/under-scaled penalty transfers value between borrower and liquidator inconsistently with the approved risk curve, which — since it is triggered on every liquidation of affected egroups, not a one-off edge case — represents a systemic miscalculation in the accounting of debt/collateral seized during liquidation (temporary freezing/incorrect distribution of funds tied to the position).

This lands in the **High** impact bucket (temporary freezing of funds / distortion of liquidation accounting); it could escalate toward **Critical** insolvency risk if an egroup is configured with a non-multiple-of-BPS aggressive exponent and undercollateralized debt cannot be fully liquidated as designed.

### Likelihood Explanation
The trigger condition (a `LIQ-CURVE-EXP` value that isn't an exact multiple of 10000) is well within the documented valid parameter space and is exactly the kind of value the curve feature is designed for (gentle/aggressive curves other than exactly linear or exactly sqrt). No adversarial or privileged action is required beyond normal liquidation calls by any market participant once such an egroup exists.

### Recommendation
Replace the integer-truncating exponent handling with fixed-point exponentiation (e.g. `pow` on a properly scaled fractional base, or a lookup/interpolation table) so that `calc-liq-factor-exp` correctly implements `factor^(exp/BPS)` for arbitrary configured exponents, not only exact multiples of `BPS` or exactly `0.5`. Add explicit tests covering non-multiple exponents (e.g. 3000, 7000, 12500, 15000 bps) to ensure the curve output matches the documented behavior, and remove/replace the `;; assume factor^0.5` shortcut with the general formula.

### Proof of Concept
1. DAO configures an egroup with `LIQ-CURVE-EXP = u15000` (a valid, in-range "aggressive curve" value per `docs/egroups.md`).
2. A borrower's position in that egroup becomes partially liquidatable (`ltv-curr` between `LTV-LIQ-PARTIAL` and `LTV-LIQ-FULL`).
3. Any liquidator calls `liquidate` against the position; internally `calc-liq-factor-exp` is invoked with `exp = 15000`, `(/ 15000 10000) = 1`, producing `liq-pct-scaled = factor` (identical to the linear case), instead of the intended steeper aggressive curve.
4. The resulting `max-debt-usd` and `liq-penalty` differ from the values the DAO intended when setting `LIQ-CURVE-EXP = 15000`, silently under/over-liquidating the position relative to the approved risk model — reproducible deterministically for any exponent not equal to a multiple of `10000` (or, in the `<BPS` branch, not equal to `5000`). [1](#0-0)

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

**File:** mainnet/contracts/market/v0-4-market.clar (L736-753)
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
```
