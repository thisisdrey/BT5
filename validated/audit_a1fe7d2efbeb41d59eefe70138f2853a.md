### Title
Graduated liquidation penalty is recomputed from post-liquidation LTV, letting a liquidator split one liquidation into an atomic sequence that extracts more total penalty/collateral than a single liquidation and pushes the borrower's position deeper into insolvency - (File: `mainnet/contracts/market/v0-4-market.clar`)

### Summary
The Zest liquidation penalty (`liq-penalty`) and the liquidatable-debt cap (`liq-pct-scaled`) are derived from the position's *current* LTV, recomputed fresh on every `liquidate` call via `calc-liquidation-params`. Because the liquidator's bonus is paid in collateral relative to debt, a partial liquidation can, once LTV is high enough, *increase* rather than decrease the residual LTV. An attacker can therefore split one large liquidation into several smaller ones executed atomically (e.g. via `liquidate-multi` targeting the same borrower repeatedly, or a wrapper contract calling `liquidate` several times in one transaction) so that each subsequent call sees a higher `current-ltv` than the previous one, yielding a higher `liq-penalty` on that slice. This mirrors the Ajna H-1 root cause: a reward/penalty rate recomputed from a state variable that the mechanism's own prior execution has worsened, letting atomic sequential calls extract more value in total than one execution would under the protocol's intended graduated-penalty curve.

### Finding Description
`calc-liquidation-params` computes, from the live `current-ltv`: [1](#0-0) 
- `liq-pct-linear`/`liq-pct-scaled` — the fraction of debt eligible to be repaid this call, and
- `liq-penalty` — the bonus (bps) paid to the liquidator, linearly (or curve-)interpolated between `LIQ-PENALTY-MIN` and `LIQ-PENALTY-MAX` as `current-ltv` moves from `LTV-LIQ-PARTIAL` to `LTV-LIQ-FULL`: [2](#0-1) 

Both values are read entirely from the *live* position state in `liquidate`: [3](#0-2) 

After execution, the position's debt is reduced by `debt-to-repay` and collateral by `coll-final = debt-to-repay * (1+liq-penalty)` (in USD terms), i.e. collateral is removed in *larger* proportion than debt whenever `liq-penalty > 0`: [4](#0-3) 

Let `D`/`C` be debt/collateral USD before a partial liquidation of `dR` debt with penalty `p`. New LTV is `(D-dR)/(C-dR(1+p))`. This is algebraically **greater** than the old LTV `D/C` exactly when `D/C > 1/(1+p)`. Since `LIQ-PENALTY-MAX` is up to 10% (`p=0.10` ⇒ crossover at LTV≈90.9%) and `LTV-LIQ-FULL` is typically 95% (per the example egroup configs) [5](#0-4) 
there is a real LTV band (between the crossover point and `LTV-LIQ-FULL`) where a partial liquidation call *raises* the residual LTV instead of curing it. Because `liq-penalty` is monotonically increasing in `current-ltv` via `calc-liq-factor`/`calc-liq-factor-bound`, each subsequent atomic call in that band sees a strictly higher LTV and therefore a strictly higher `liq-penalty` than the previous call — the liquidator captures a growing bonus rate with each slice, and the aggregate collateral extracted for the same total debt repaid exceeds what one single (larger) liquidation at the original LTV would have paid out under the intended monotone-cure design of `calc-liquidation-params`.

`liquidate-multi` allows exactly this kind of atomic, repeated invocation (including, structurally, of the same borrower/asset pair multiple times in one transaction), because each entry is independently dispatched through `call-liquidate` inside a single `map`: [6](#0-5) 
Even without `liquidate-multi`, any contract-based liquidator can call `liquidate` several times in one Stacks transaction against the same borrower.

### Impact Explanation
This is a **temporary freezing of funds / worsened insolvency** analog to the in-scope impact classes: each split liquidation both (a) extracts a disproportionately large collateral bonus from the borrower's remaining collateral relative to debt repaid, at the reserves'/protocol's and remaining lenders' expense, and (b) can leave the position with a *higher* residual LTV than before liquidation began, accelerating it toward `LTV-LIQ-FULL`/insolvency and increasing the chance of un-collateralized bad debt that must be socialized (`bad-debt-socialized` path). This worsens collateralization of the borrower's position beyond what the graduated-liquidation design intends, directly paralleling the Ajna H-1 impact ("worsen the collaterization of the auctioned loan") and can compound into protocol-level bad debt if repeated near `LTV-LIQ-FULL`.

### Likelihood Explanation
Exploitation requires only an ordinary permissionless liquidator (no privileged role, no DAO action) calling `liquidate` (or `liquidate-multi`) multiple times atomically once a position's LTV is within the crossover band described above — a condition that occurs naturally for any position with a non-trivial `LIQ-PENALTY-MAX` before it reaches `LTV-LIQ-FULL`. No oracle manipulation or flashloan is needed; a normal debt-repay/collateral-seize flow with a moderate gas cost increase (splitting a take into a batch) suffices, exactly as in the original Ajna report.

### Recommendation
Cap or bound the liquidation-penalty computation so a single liquidation (or a rapid, atomic sequence of liquidations against the same position) cannot make the residual LTV worse than before the liquidation began — e.g., recompute/verify post-liquidation LTV ≤ pre-liquidation LTV within `liquidate`, or bound `liq-penalty`/`liq-pct-scaled` such that `debt-repay * (1+liq-penalty) ≤ debt-repay * C/D` is guaranteed to reduce LTV. Alternatively, memorialize the LTV/penalty parameters read at the start of a borrower's liquidation sequence (similar to Ajna's fix of memorializing TP at kick) so a caller cannot benefit from LTV movement they themselves cause within the same block/transaction.

### Proof of Concept
Numeric illustration using the example egroup parameters (`LTV-LIQ-PARTIAL=8500`, `LTV-LIQ-FULL=9500`, `LIQ-PENALTY-MIN=500`, `LIQ-PENALTY-MAX=1000`, `LIQ-CURVE-EXP=20000`): [5](#0-4) 

1. Position starts at `D0 = 9400`, `C0 = 10000` USD ⇒ LTV = 94.0% (inside partial→full band, above the crossover for the resulting penalty).
2. Call `liquidate` for a small `debt-amount` (e.g. `dR1 = 100` USD). `calc-liquidation-params` at LTV=94% yields some `liq-penalty1` (bounded up to 10% by curve `20000`). Since `94% > 1/(1+liq-penalty1)` for any `liq-penalty1` above the crossover value, new `D1 = D0 - dR1`, `C1 = C0 - dR1*(1+liq-penalty1)` produces `D1/C1 > D0/C0` (LTV increases).
3. Immediately (same tx, e.g. via `liquidate-multi` or a wrapper contract) call `liquidate` again on the same borrower with another small `dR2`. `calc-liquidation-params` now reads the higher `current-ltv = D1/C1`, producing `liq-penalty2 > liq-penalty1` via the monotone `calc-liq-factor`/`calc-liq-factor-bound` chain [1](#0-0) 
so the liquidator earns a strictly larger bonus rate on this slice than it would have on an equivalent-sized slice of one single big liquidation executed at `D0/C0`.
4. Repeating this splitting throughout the crossover band lets the liquidator accumulate more total collateral bonus for the same aggregate debt repaid than a single non-split liquidation call would yield, while also leaving the position's LTV higher (closer to/at `LTV-LIQ-FULL`) than a correctly-designed one-shot liquidation would.

Full on-chain confirmation (exact numeric magnitudes per test harness in `local-testing/tests/flows/liquidation/liquidation-basic.test.ts`) would require running a Clarinet/Vitest simulation with two or more sequential `market.liquidate` calls on the same borrower inside one transaction; the index does not contain a ready-made multi-call splitting test, so this PoC is derived analytically from the cited formulas rather than an executed test run.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L701-719)
```text
;; Calculate liquidation factor: ((ltv-curr - ltv-liq-partial) * BPS) / (ltv-liq-full - ltv-liq-partial)
;; Capped at BPS (100%) to prevent over-liquidation
(define-private (calc-liq-factor (ltv-curr uint) (ltv-liq-partial uint) (ltv-liq-full uint))
  (min BPS (div-bps-down (- ltv-curr ltv-liq-partial) (- ltv-liq-full ltv-liq-partial))))

;; Apply curve exponent for graduated liquidation
;; liq-factor = liq-factor^alpha
(define-private (calc-liq-factor-exp (factor uint) (exp uint))
  (if (is-eq exp BPS) 
    factor
    (if (> exp BPS) 
        (/ (pow factor (/ exp BPS)) (pow BPS (- (/ exp BPS) u1)))
        (sqrti (* factor BPS))))) ;; assume factor^0.5

;; Scale penalty between min and max using liquidation factor
;; liq-penalty = liq-penalty-min + (liq-factor * (liq-penalty-max - liq-penalty-min) / BPS)
;; Capped at bound-max to handle cases where liq-factor > BPS
(define-private (calc-liq-factor-bound (liq-factor uint) (bound-min uint) (bound-max uint))
  (min bound-max (+ bound-min (mul-bps-down liq-factor (- bound-max bound-min)))))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L726-734)
```text
;; Calculate collateral to seize (includes liquidator bonus)
;; collateral-repay = debt-repay * (BPS + liq-penalty) / BPS
(define-private (calc-liq-collateral-repay (debt-repay uint) (liq-penalty uint)) 
  (mul-bps-down debt-repay (+ BPS liq-penalty)))

;; Calculate actual debt repayment when collateral is capped
;; debt-repay-real = (collateral-amount-usd * BPS) / (BPS + liq-penalty)
(define-private (calc-liq-debt-repay-real (collateral-amount-usd uint) (liq-penalty uint)) 
  (div-bps-down collateral-amount-usd (+ BPS liq-penalty)))
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

**File:** mainnet/contracts/market/v0-4-market.clar (L1422-1444)
```text
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
                  curve-exponent total-debt-usd))
    (liq-pct-scaled (get liq-pct-scaled liq-params))
    (liq-penalty (get liq-penalty liq-params))
    (max-debt-usd (get max-debt-usd liq-params))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1587-1599)
```text
;; Liquidates multiple positions atomically
;; Each position can have different: borrower, collateral asset, debt asset, and debt amount
;; Prevents front-running attacks that prevent bad debt socialization
;; Note: price-feeds not supported in batch - update prices separately or use individual liquidate()
;; Returns list of responses - one per position (ok/err)
;; Failed liquidations return error codes but don't revert entire batch
(define-public (liquidate-multi
                (positions (list 64 { borrower: principal,
                                      collateral-ft: <ft-trait>,
                                      debt-ft: <ft-trait>,
                                      debt-amount: uint,
                                      min-collateral-expected: uint })))
  (ok (map call-liquidate positions)))
```

**File:** local-testing/contracts/proposals/proposal-create-egroup-sbtc-usdc.clar (L14-19)
```text
      LIQ-CURVE-EXP: u20000,              ;; 2.0 quadratic (gentle-then-steep)
      LIQ-PENALTY-MIN: u500,              ;; 5%
      LIQ-PENALTY-MAX: u1000,             ;; 10%
      LTV-BORROW: u7000,                  ;; 70% max borrow LTV
      LTV-LIQ-PARTIAL: u8500,             ;; 85% partial liquidation threshold
      LTV-LIQ-FULL: u9500                 ;; 95% full liquidation threshold
```
