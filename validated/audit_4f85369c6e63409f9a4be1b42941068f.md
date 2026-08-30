Based on my research, the Zest Protocol is a Stacks-based lending/borrowing market (collateral, debt, liquidation via LTV thresholds), not a leveraged perpetual-trading protocol. The external report's bug class is specific to gTrade-style leverage trading: it concerns `IncreasePositionSizeUtils.prepareCallbackValues` recalculating a new liquidation price using a *hypothetical future* `newCollateralAmount`/`newLeverage` pair to derive a borrowing fee, which overstates/understates the fee versus the trade's actual accrued borrowing fee, and can cause a legitimate size-increase callback to revert incorrectly.

There is no analogous concept in this codebase:
- Zest's market has no "leverage," "position size increase," or per-trade "borrowing fee" tied to a leverage multiplier; interest/debt accrual is via a global borrow index (`get-cached-indexes`, `INDEX-PRECISION`) applied uniformly to a user's scaled debt, not recomputed from a hypothetical "new" collateral/leverage pair. [1](#0-0) 
- Liquidation math (`calc-liquidation-params`, `process-debt-asset`, `process-collateral-asset`, `calc-final-liquidation-amounts`) is computed purely from current on-chain state (current debt, current collateral balances, current oracle prices) at the time of the call — there is no "future collateral/future leverage" substitution used to derive a fee that then feeds into a liquidation-price check. [2](#0-1) 
- The one place that does use a "future" hypothetical value is the capacity check in `collateral-add`, which compares `future-capacity` against `current-capacity` to ensure adding new collateral doesn't reduce borrowing capacity — this is a deliberate safety design (an LTV/capacity gate), not a fee-miscalculation feeding into a liquidation-price/callback validation flow, and doesn't inflate a "borrowing fee" the way the report describes.
<invoke name="grep_search">
<parameter name="pattern">newLeverage|newCollateralAmount|prepareCallbackValues|IncreasePositionSize</parameter>
<parameter name="repo_name">Lauraivanka/zest-v2-contracts--022</parameter>
</invoke>

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L856-870)
```text
;; Converts to scaled units, caps at current debt, calculates final collateral
;; Returns: { scaled-to-remove: uint, debt-to-repay: uint, coll-final: uint }
(define-private (scale-debt-for-liquidation
  (debt-final uint)
  (coll-actual uint)
  (curr-scaled uint)
  (asset-id uint))
  (let (;; convert debt amount to scaled units for storage
        (borrow-index (get index (unwrap-panic (get-cached-indexes asset-id))))
        (scaled-debt (mul-div-down debt-final INDEX-PRECISION borrow-index))
        ;; cap at current debt (prevent over-repayment)
        (scaled-to-remove (if (> scaled-debt curr-scaled) curr-scaled scaled-debt))
        (debt-to-repay (mul-div-up scaled-to-remove borrow-index INDEX-PRECISION))
        ;; If debt was capped, scale collateral proportionally
        (coll-final (if (< scaled-to-remove scaled-debt)
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1436-1467)
```text

    ;; liquidation parameters (graduated liquidation calculation)
    (liq-params (calc-liquidation-params 
                  current-ltv ltv-liq-partial ltv-liq-full
                  liq-penalty-min liq-penalty-max 
                  curve-exponent total-debt-usd))
    (liq-pct-scaled (get liq-pct-scaled liq-params))
    (liq-penalty (get liq-penalty liq-params))
    (max-debt-usd (get max-debt-usd liq-params))

    ;; debt processing
    (debt-info (process-debt-asset debt-amount debt-aid max-debt-usd assets))
    (debt-actual-usd (get debt-actual-usd debt-info))
    (debt-actual (get debt-actual debt-info))
    (debt-price (get debt-price debt-info))
    (debt-decimals (get debt-decimals debt-info))

    ;; collateral processing
    (user-coll-balance (find-collateral-amount (get collateral pos-full) coll-aid))
    (coll-info (process-collateral-asset coll-aid debt-actual-usd liq-penalty 
                                         user-coll-balance assets coll-asset))
    (coll-actual (get coll-actual coll-info))
    (coll-expected (get coll-expected coll-info))
    (coll-price (get coll-price coll-info))
    (coll-decimals (get coll-decimals coll-info))

    ;; final liquidation amounts (with proportional adjustment if needed)
    (final-amounts (calc-final-liquidation-amounts
                     debt-actual-usd coll-actual coll-expected
                     coll-price coll-decimals
                     debt-price debt-decimals liq-penalty))
    (debt-final-usd (get debt-final-usd final-amounts))
```
