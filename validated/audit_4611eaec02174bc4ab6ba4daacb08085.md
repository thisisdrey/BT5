### Title
Liquidation entry point (`liquidate`) is fully blocked by the Pyth confidence-interval gate (`check-confidence`), stalling liquidation of under-margined positions exactly when oracle volatility is highest - ([File: mainnet/contracts/market/v0-4-market.clar])

### Summary
`v0-4-market.clar`'s `liquidate` function resolves prices for *all* assets in the borrower's egroup mask (`get-assets` / `get-notional-evaluation`) before computing health and executing the seizure. Every Pyth-backed price resolution passes through `check-confidence`, which hard-reverts the *entire* call with `ERR-PRICE-CONFIDENCE-LOW` if the reported confidence interval exceeds `max-confidence-ratio` (default 10%) of the price, for **any** asset involved — including assets that aren't even the specific collateral/debt pair the liquidator is targeting. This is structurally the same failure mode as the GTE Perps divergence-band bug: a price-quality gate that is supposed to protect against bad data instead becomes an all-or-nothing switch that halts the entire liquidation path, and it is most likely to trip precisely during the volatile conditions that make liquidations urgent (Pyth confidence intervals widen during high volatility/thin liquidity).

### Finding Description
`check-confidence` is defined as: [1](#0-0) 

It is invoked unconditionally inside `resolve-pyth`, which is the price-feed resolver used for every Pyth-quoted asset: [2](#0-1) 

`resolve-pyth` is reached from `resolve-price-feed` → `price-resolve`, and `price-resolve` propagates the confidence failure via `try!`/`asserts!` up the call stack with no fallback or graceful degradation: [3](#0-2) 

`liquidate` calls `get-assets mask` and `get-notional-evaluation` to price *every* enabled collateral/debt asset in the borrower's egroup before it can even reach the health check: [4](#0-3) 

If confidence data for *any* one of those assets (not necessarily the target collateral/debt pair) momentarily exceeds the 10% band — which is exactly what happens on Pyth during fast markets, thin liquidity, or feed congestion — the whole `try!`/`let`-bound resolution chain aborts, `liquidate` reverts with `ERR-PRICE-CONFIDENCE-LOW`, and no partial or backstop path exists to still perform the liquidation. This mirrors the GTE Perps flaw exactly: a legitimate price-integrity gate (divergence band there, confidence ratio here) is applied uniformly with no bypass for the liquidation path, so the very market conditions that produce under-margined, need-to-liquidate positions (volatility, thin liquidity) are the same conditions that widen Pyth's confidence interval and disable the gate that would otherwise let liquidation proceed.

Unlike deliberate safety features excluded by scope (e.g., pausing during a DAO-declared liquidation-grace-period, or same-block borrow protection), this is an unconditional per-call oracle-quality assertion with no fallback (no DIA fallback, no boundary-clamped price, no confidence-based penalty) and it silently DoS's the entire liquidation call for the borrower's whole position, not just the specific stale feed.

### Impact Explanation
This lands on **temporary freezing of funds** (and risks graduating to **protocol insolvency**): while `ERR-PRICE-CONFIDENCE-LOW` persists, liquidators cannot execute `liquidate` against the under-margined account. The position's debt keeps growing via interest accrual while collateral value can keep falling, and once liquidation eventually becomes possible again (confidence narrows), the account may already be under water, forcing bad-debt socialization via `ERR-BAD-DEBT-SOCIALIZATION-FAILED`/socialize-debt logic, which spreads losses across other lenders. This is a systemic risk analogous to the auto-deleveraging/insurance-fund drain described in the original report, translated to Zest's lending-market context.

### Likelihood Explanation
Likelihood is moderate-to-high: Pyth confidence intervals widen naturally during high volatility and thin liquidity windows — the exact conditions under which positions cross into liquidatable LTV. No attacker action or malicious oracle publisher is required (oracle-publisher and third-party data quality issues are out of scope, but the *protocol's own* uniform application of the confidence gate to the liquidation entry point, with zero bypass, is an in-scope root cause). Any principal attempting `liquidate` during a volatility spike on any one relevant asset in the egroup will trigger this.

### Recommendation
- Do not let a confidence-interval failure on a *different* asset in the egroup silently block liquidation of the target debt/collateral pair; scope the confidence check to only the assets actually used to seize/repay, or make degraded-confidence assets exempt from `liquidate`'s cross-asset revaluation when they are not part of the requested trade.
- For the specific collateral/debt pair being liquidated, allow the liquidator (or a privileged backstop path) to proceed using a bounded/clamped price (e.g., price ± confidence, biased against the liquidator) instead of an outright revert, similar to the "execute at band boundary with explicit penalty" mitigation suggested in the analogous report.
- Alternatively, provide a DAO-governed emergency price path (already partially present via DIA/mock oracle types) that can be substituted specifically for liquidation when Pyth confidence is degraded, so liquidation is never fully blocked network-wide.

### Proof of Concept
1. Borrower's position becomes liquidatable (`current-ltv >= ltv-liq-partial`).
2. Market undergoes volatility; Pyth's reported `conf` for some asset priced in the borrower's egroup mask (could be collateral, debt, or even an unrelated enabled asset counted in `get-assets`) exceeds `max-confidence-ratio` (10% of price) as seen at `mainnet/contracts/market/v0-4-market.clar:305-306`.
3. Liquidator calls `liquidate(borrower, collateral-ft, debt-ft, debt-amount, min-collateral-expected, none, none)`.
4. Inside `liquidate`, `get-assets mask` / `get-notional-evaluation` resolves prices for all egroup assets via `price-resolve` → `resolve-pyth` → `check-confidence`, per `mainnet/contracts/market/v0-4-market.clar:1409-1436` and `:305-320`.
5. `check-confidence` fails, `asserts!` returns `ERR-PRICE-CONFIDENCE-LOW`, `try!` propagates the error, and the entire `liquidate` call reverts with no state changes.
6. The under-margined position remains open and continues accruing debt/losing collateral value until confidence narrows again, potentially resulting in bad debt requiring socialization.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L305-306)
```text
(define-private (check-confidence (price int) (confidence uint))
  (ok (asserts! (<= confidence (/ (* (to-uint price) (var-get max-confidence-ratio)) BPS)) ERR-PRICE-CONFIDENCE-LOW)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L312-320)
```text
(define-private (resolve-pyth (ident (buff 32)))
  (let ((response (try! (call-pyth ident)))
        (price (get price response))
        (expo (get expo response))
        (conf (get conf response))
        (final-price (normalize-pyth price expo))
        (timestamp (get publish-time response)))
    (try! (check-confidence price conf))
    (ok { value: final-price, timestamp: timestamp })))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L373-395)
```text
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

**File:** mainnet/contracts/market/v0-4-market.clar (L1409-1436)
```text
    ;; NOW safe to resolve prices (cache is populated)
    (assets (get-assets mask))
    (notional-valued-assets (get-notional-evaluation { position: position, assets: assets }))
    (total-collateral-usd (get collateral notional-valued-assets))
    (total-debt-usd (get debt notional-valued-assets))

    ;; LTC thresholds, liq params, health
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

```
