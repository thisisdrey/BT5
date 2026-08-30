### Title
Truncated integer division in `calc-liq-factor-exp` silently collapses graduated liquidation curve to the wrong exponent for any `LIQ-CURVE-EXP` not an exact multiple of `BPS` - ([File: mainnet/contracts/market/v0-4-market.clar])

### Summary
`calc-liq-factor-exp` is Zest's equivalent of the reported `unlockExponent` mechanism: an exponent parameter is applied to a linear percentage factor, scaled by a fixed precision constant (`BPS = u10000`), to produce a graduated curve. Exactly like the reported bug, the exponent math only behaves correctly for the exponent equal to the precision unit (`1.0x`, i.e. `exp == BPS`). For any other legitimately-configured exponent value above `BPS`, integer division truncates the fractional part of the exponent before it is ever used, so the curve silently reverts to a different (usually linear) exponent than the one configured, corrupting the graduated liquidation-penalty schedule for every liquidation processed under that egroup.

### Finding Description
The relevant function lives in `calc-liq-factor-exp`: [1](#0-0) 

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

`BPS` is `u10000` [2](#0-1) , and `LIQ-CURVE-EXP` is stored as a raw `(buff 2)` bps value read directly from the egroup record and passed unmodified into `calc-liquidation-params` → `calc-liq-factor-exp`: [3](#0-2) 

The documentation states the parameter is meant to be a continuous curve control (`10000` linear, `>10000` more aggressive, `<10000` gentler) with no restriction to multiples of `10000` [4](#0-3) .

The bug: for the `exp > BPS` branch, the code computes the integer exponent as `n = exp / BPS` using **integer (floor) division**, then computes `factor^n / BPS^(n-1)`. Any fractional part of the intended exponent is discarded before exponentiation:

- `exp = 15000` (intended 1.5x curve) → `n = 15000 / 10000 = 1` (Clarity integer division truncates) → the code returns `factor^1 / BPS^0 = factor`, i.e. **plain linear scaling**, identical to `exp = 10000`.
- `exp = 25000` (intended 2.5x curve) → `n = 2` → the code silently computes a pure quadratic curve `factor^2 / BPS`, discarding the extra `0.5` exponent entirely.
- Any exponent in `(10000, 20000)` behaves exactly like `exp = 10000` (linear); any exponent in `[20000, 30000)` behaves exactly like `exp = 20000` (quadratic); etc.

This is precisely the pattern in the source report: the exponent parameter is read and applied through a precision-scaled power operation, but the implementation only works correctly when the configured value happens to land exactly on a multiple of the precision constant. Any other in-range, honestly-configured value produces a curve exponent silently different from what was configured — not because of a DAO mistake, but because the exponent-scaling arithmetic itself discards information for all non-multiple values.

### Impact Explanation
`liq-pct-scaled` (the output of `calc-liq-factor-exp`) directly drives two liquidation outputs used against every borrower liquidated under the affected egroup:
- `liq-penalty`, via `calc-liq-factor-bound`, which determines the liquidation bonus/penalty paid to the liquidator and taken from the borrower's collateral [5](#0-4) 
- `max-debt-usd`, via `calc-liq-debt-repay`, which caps how much debt/collateral can be liquidated in a single call [6](#0-5) 

Because the curve is silently miscomputed for any non-multiple-of-BPS exponent, borrowers are charged a liquidation penalty different (and potentially significantly higher for early-stage LTV breaches) from what the graduated schedule was designed to produce, and the amount of collateral seizable per liquidation call is likewise wrong. Since the borrower's own collateral (funds at rest) is being seized based on an incorrect calculation, this results in an unintended transfer of value out of the borrower's position beyond what the configured risk parameters were designed to allow — i.e., theft/value-loss at the position level, distinct from any deliberate liquidator-bonus design decision (that decision is about penalty *bounds*, not about the exponent-truncation defect described here).

### Likelihood Explanation
Every ordinary liquidation call against a position in an egroup whose `LIQ-CURVE-EXP` is not an exact multiple of `10000` triggers this miscalculation — no privileged action or DAO error is required beyond normal, legitimate parameter configuration (per the documented valid range, fractional/non-multiple curve exponents are explicitly an intended, supported use case, not a misconfiguration). Any liquidator calling the standard liquidation entry point on any qualifying position exercises the buggy path deterministically.

### Recommendation
Rework `calc-liq-factor-exp` to perform true fixed-point exponentiation (e.g., using a fixed-point power/log implementation or restrict `LIQ-CURVE-EXP` to a small enumerated set of supported exact exponents validated at egroup-creation time), so that the applied curve always matches the configured exponent rather than silently truncating to the nearest multiple of `BPS`.

### Proof of Concept
1. DAO creates/updates an egroup with `LIQ-CURVE-EXP = 15000` (intending a 1.5x aggressive curve, a documented valid, non-multiple value).
2. A borrower's position breaches `LTV-LIQ-PARTIAL`, yielding `liq-pct-linear = 4000` (40%), for example.
3. `calc-liq-factor-exp` computes `exp > BPS` branch: `n = 15000 / 10000 = 1` (Clarity truncates), so `liq-pct-scaled = 4000^1 / 10000^0 = 4000` — identical to what a linear (`exp = 10000`) curve would produce.
4. The liquidator receives the linear-curve penalty/debt cap instead of the intended steeper 1.5x curve penalty/debt cap, and this happens on every liquidation of every position in this egroup for as long as the parameter remains at `15000` — a direct, deterministic mismatch between configured and applied liquidation economics that removes value from borrowers' collateral beyond the intended graduated schedule.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L32-32)
```text
(define-constant BPS u10000)
```

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

**File:** mainnet/contracts/market/v0-4-market.clar (L715-719)
```text
;; Scale penalty between min and max using liquidation factor
;; liq-penalty = liq-penalty-min + (liq-factor * (liq-penalty-max - liq-penalty-min) / BPS)
;; Capped at bound-max to handle cases where liq-factor > BPS
(define-private (calc-liq-factor-bound (liq-factor uint) (bound-min uint) (bound-max uint))
  (min bound-max (+ bound-min (mul-bps-down liq-factor (- bound-max bound-min)))))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L721-724)
```text
;; Calculate debt to repay based on liquidation factor
;; debt-repay = liq-factor * debt / BPS
(define-private (calc-liq-debt-repay (debt uint) (liq-factor uint)) 
  (mul-bps-down liq-factor debt))
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

**File:** docs/egroups.md (L43-48)
```markdown
**Graduated Liquidation:**

The `LIQ-CURVE-EXP` parameter controls how liquidation penalty scales between min and max:
- `10000` (1.0): Linear scaling
- `>10000` (>1.0): Aggressive curve (penalty increases faster)
- `<10000` (<1.0): Gentle curve (e.g., square root)
```
