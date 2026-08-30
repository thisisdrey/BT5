## Analysis

The reported bug class is: **reusing a single fee/curve formula uniformly across configurations that require materially different math, causing the formula to silently diverge from the intended curve.** The Zest analog is the **graduated liquidation curve exponent calculation** `calc-liq-factor-exp` in the market contract, which is documented to support fine‑grained (bps) exponent tuning per egroup but whose implementation silently collapses to the wrong curve for the vast majority of that configurable range due to integer‑division truncation of the exponent.

### Title
Graduated liquidation curve silently collapses to linear for non-multiple-of-BPS `LIQ-CURVE-EXP` values, producing wrong liquidation penalty/factor - (File: mainnet/contracts/market/v0-4-market.clar)

### Summary
`LIQ-CURVE-EXP` is documented and stored as a **bps-precision** parameter (e.g. `12500` = 1.25x, `20000` = 2.0x) meant to let the DAO tune how aggressively the liquidation penalty/factor scales between the partial and full liquidation LTV thresholds [1](#0-0) . The implementation in `calc-liq-factor-exp`, however, performs integer division `(/ exp BPS)` to derive the integer power, which truncates any exponent that is not an exact multiple of `BPS` (`10000`) down to `1`, causing `factor^1` to be returned — i.e. the same result as the pure linear case `exp = BPS` — for the entire range `10001..19999` (and analogously for every non-multiple band above `20000`, `30000`, etc.).

### Finding Description
`calc-liq-factor-exp` is:
```
(define-private (calc-liq-factor-exp (factor uint) (exp uint))
  (if (is-eq exp BPS) 
    factor
    (if (> exp BPS) 
        (/ (pow factor (/ exp BPS)) (pow BPS (- (/ exp BPS) u1)))
        (sqrti (* factor BPS))))) ;; assume factor^0.5
``` [2](#0-1) 

For `exp > BPS`, Clarity's `pow` only accepts integer exponents, so the code approximates `factor^(exp/BPS)` by first integer-dividing `exp` by `BPS`. Because Clarity `uint` division truncates, any `exp` in `(10000, 19999]` yields `(/ exp BPS) = 1`, so the branch computes `(pow factor 1) / (pow BPS 0)` = `factor` — mathematically identical to the `is-eq exp BPS` (linear, exponent 1.0) branch. The same collapse recurs for every band between successive multiples of `BPS` (e.g. `20001..29999` collapses to the `exp=20000` curve). Additionally, the `exp < BPS` branch ignores the actual exponent value entirely and always applies a fixed square-root curve (`sqrti`) regardless of what fractional exponent (e.g. `5000` vs `9000`) was configured, per the comment `;; assume factor^0.5`.

This function feeds directly into `calc-liquidation-params`, which every `liquidate` call invokes to compute `liq-pct-scaled`, `liq-penalty`, and `max-debt-usd` — the exact amounts of debt repayable and collateral seized in a liquidation [3](#0-2) . The `liquidate` public entry point (callable by any ordinary principal) uses these outputs to size the repay/seize amounts [4](#0-3) .

Any egroup that the DAO configures with a `LIQ-CURVE-EXP` intended to be a fine-grained non-linear curve (anything other than an exact multiple of `10000`) will, in practice, silently execute a different (linear, or wrong-band) curve than intended, without any error or revert.

### Impact Explanation
Because the effective liquidation-factor/penalty curve diverges from the DAO-configured curve for the (very likely, given bps granularity) common case of a non-multiple-of-`10000` exponent, every liquidation executed against positions in an affected egroup computes an incorrect `liq-penalty` and `max-debt-usd`. Depending on whether the true intended curve was more or less aggressive than the collapsed (linear or wrong-band) result actually used, this systematically over- or under-rewards liquidators with collateral relative to the debt they repaid, extracting excess collateral value from borrowers being liquidated beyond what the protocol's intended risk parameters dictate. This is an ordinary, unprivileged liquidator interacting with the standard `liquidate` entry point — no privileged access or DAO misconfiguration is required, only a legitimately DAO-set fine-grained exponent, which the code fails to honor. This lands in theft of unclaimed yield (excess collateral extracted via mispriced liquidation penalties) — a High-severity impact class.

### Likelihood Explanation
Any egroup created with a `LIQ-CURVE-EXP` that is not an exact multiple of `10000` (the natural way to express e.g. a 1.25x or 1.75x curve in bps) triggers the bug on every single liquidation against positions in that egroup, with no additional preconditions — extremely high likelihood given the documented intent of bps-level granularity.

### Recommendation
Rework `calc-liq-factor-exp` to correctly evaluate fractional exponents (e.g. via a fixed-point power approximation, or by explicitly restricting/validating `LIQ-CURVE-EXP` to only the exact supported set of exponents and rejecting/rounding any other value at egroup-creation time so configuration intent matches runtime behavior).

### Proof of Concept
1. DAO (or any proposal) creates an egroup with `LIQ-CURVE-EXP: u15000` (intending a 1.5x aggressive penalty curve) via `egroup insert` [5](#0-4) .
2. A borrower's position in that egroup becomes liquidatable (`current-ltv >= ltv-liq-partial`).
3. Any unprivileged liquidator calls `liquidate`; the market computes `curve-exponent = 15000` and passes it into `calc-liquidation-params` → `calc-liq-factor-exp` [6](#0-5) .
4. Inside `calc-liq-factor-exp`, `(/ u15000 u10000)` evaluates to `u1`, so the function returns `factor` unchanged — identical to what would happen if `LIQ-CURVE-EXP` had been set to `u10000` (pure linear), silently discarding the intended 1.5x curve shape.
5. The resulting `liq-penalty` and `max-debt-usd` used to size the liquidator's collateral seizure differ from the DAO-intended curve for every liquidation in that egroup, systematically mispricing liquidations.

### Citations

**File:** docs/egroups.md (L41-48)
```markdown
| `LIQ-CURVE-EXP` | bps | 10000 (1.0) | Exponent for graduated penalty curve |

**Graduated Liquidation:**

The `LIQ-CURVE-EXP` parameter controls how liquidation penalty scales between min and max:
- `10000` (1.0): Linear scaling
- `>10000` (>1.0): Aggressive curve (penalty increases faster)
- `<10000` (<1.0): Gentle curve (e.g., square root)
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

**File:** mainnet/contracts/market/v0-4-market.clar (L1416-1440)
```text
    (ltv-liq-partial (buff-to-uint-be (get LTV-LIQ-PARTIAL group)))
    (ltv-liq-full (buff-to-uint-be (get LTV-LIQ-FULL group)))
    (liq-penalty-min (buff-to-uint-be (get LIQ-PENALTY-MIN group)))
    (liq-penalty-max (buff-to-uint-be (get LIQ-PENALTY-MAX group)))
    (curve-exponent (buff-to-uint-be (get LIQ-CURVE-EXP group)))

    ;; LTV = (debt x 10,000) / collateral
    ;; handle edge case: If collateral = 0, return max LTV (BPS) or 0 if debt also 0
    (current-ltv   (if (is-eq total-collateral-usd u0)
                       (if (is-eq total-debt-usd u0) u0 BPS)
                       (mul-div-down total-debt-usd BPS total-collateral-usd)))
    
    ;; Oracle frontrunning protection: prevent same-block liquidation
    ;; This blocks flash-loan based attacks where user borrows + gets liquidated in same block
    (last-borrow-block (get last-borrow-block position))
    (same-block-check (asserts! (not (is-eq last-borrow-block stacks-block-height)) ERR-LIQUIDATION-BORROW-SAME-BLOCK))

    ;; health check (FAIL-FAST) 
    ;; Check position is liquidatable BEFORE calling calc-liq-factor
    (health-check  (asserts! (>= current-ltv ltv-liq-partial) ERR-HEALTHY))

    ;; liquidation parameters (graduated liquidation calculation)
    (liq-params (calc-liquidation-params 
                  current-ltv ltv-liq-partial ltv-liq-full
                  liq-penalty-min liq-penalty-max 
```

**File:** local-testing/contracts/proposals/proposal-create-multiple-egroups.clar (L19-28)
```text
    (try! (contract-call? .egroup insert {
      MASK: u1180591620717411303428,
      BORROW-DISABLED-MASK: u0,
      LIQ-CURVE-EXP: u20000,              ;; 2.0 exponent (quadratic)
      LIQ-PENALTY-MIN: u500,              ;; 5%
      LIQ-PENALTY-MAX: u1000,             ;; 10%
      LTV-BORROW: u7000,                  ;; 70% - High confidence pair
      LTV-LIQ-PARTIAL: u8500,             ;; 85%
      LTV-LIQ-FULL: u9500                 ;; 95%
    }))
```
