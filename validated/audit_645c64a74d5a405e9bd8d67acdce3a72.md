### Title
Liquidator's collateral-seizure profit shrinks with the borrower's shrinking collateral value in undercollateralized liquidations instead of staying pegged to a fixed share of debt, disincentivizing liquidation of the most underwater positions - (File: `mainnet/contracts/market/v0-4-market.clar`)

### Summary
When a position is so far underwater that the borrower's remaining collateral is insufficient to cover the "expected" liquidator payout (debt + penalty bonus), `liquidate` recomputes the amount of debt actually repaid as a fraction of the *remaining collateral value*, rather than fixing the liquidator's absolute profit as a percentage of debt. As collateral value keeps shrinking, the liquidator's USD profit shrinks proportionally, mirroring the Revert Lend `V3Vault::_calculateLiquidation` bug where the fee was computed as a percentage of `fullValue` instead of `debt`.

### Finding Description
The intended liquidation-incentive model, per `docs/market.md`, is that liquidators repay debt and receive `debt × (1 + penalty)` worth of collateral - a bonus proportional to debt repaid [1](#0-0) .

The math is implemented in three helpers in `v0-4-market.clar`:

1. `process-collateral-asset` computes `coll-usd-expected` from `debt-actual-usd` and `liq-penalty` via `calc-liq-collateral-repay`, then caps the amount actually paid out at the user's remaining balance when the expected payout exceeds what's left: [2](#0-1) [3](#0-2) 

2. When collateral is capped (`coll-actual < coll-expected` - i.e., the position is underwater), `calc-final-liquidation-amounts` recalculates the *debt actually repaid* as a fraction of the capped collateral's USD value using `calc-liq-debt-repay-real`: [4](#0-3) 

`calc-liq-debt-repay-real(collateral-amount-usd, liq-penalty) = collateral-amount-usd × BPS / (BPS + liq-penalty)`.

So when capped, the liquidator receives `coll-actual-usd` (the borrower's full remaining collateral value) and repays `debt-final-usd = coll-actual-usd × BPS/(BPS+penalty)`. The liquidator's profit is:

`profit = coll-actual-usd − debt-final-usd = coll-actual-usd × penalty/(BPS+penalty)`

This profit is a fixed *percentage of the shrinking collateral value* (`coll-actual-usd`), not a fixed percentage of the *debt*. As the position becomes more deeply underwater and `coll-actual-usd` keeps shrinking relative to the outstanding debt, the liquidator's absolute USD profit shrinks in lock-step - exactly the pattern flagged in the Revert Lend report, where the fee was `fullValue × penaltyRate` instead of `debt × penaltyRate`.

This is invoked from the `liquidate` entry point, callable by any unprivileged principal, using the same helpers for both the normal and capped paths: [5](#0-4) [6](#0-5) 

### Impact Explanation
As with the Revert Lend finding, this breaks the intended economic design (a fixed liquidator incentive as documented) and creates a growing disincentive to liquidate the worst-off positions - precisely the positions the protocol most needs liquidated to cap bad-debt exposure. The remaining bad debt is socialized via `socialize-debt-asset` once collateral is exhausted [7](#0-6) , meaning under-liquidated, deeply underwater positions linger longer, increasing the amount of debt that must eventually be socialized across depositors - a form of temporary/permanent freezing or loss of funds for the vault's LPs, consistent with the "temporary freezing of funds" / "protocol insolvency" impact classes.

### Likelihood Explanation
This triggers automatically any time a position's collateral value falls below what's needed to fully cover `debt-actual-usd × (1+penalty)` - a routine occurrence for volatile collateral in fast-moving markets, requiring no attacker action, only market conditions and normal liquidator behavior (or the lack thereof, as they rationally skip low-profit liquidations).

### Recommendation
Fix the capped-collateral branch so that the liquidator's absolute profit is pegged to a percentage of the debt actually repaid, not a percentage of the shrinking collateral value - analogous to the Revert Lend mitigation: fix `penalty = debt-actual-usd × liq-penalty / BPS` first, then let `debt-final-usd = coll-actual-usd - penalty` (liquidator pays less than face value of collateral but keeps the full penalty bonus), with any shortfall between `debt-actual-usd` and `debt-final-usd` flowing to bad-debt socialization rather than silently reducing the liquidator's reward.

### Proof of Concept
1. Borrower has debt-actual-usd = $100, liq-penalty = 10% (1000 bps), so coll-usd-expected = $110.
2. Borrower's remaining collateral is only worth $105 (position deeply underwater). `coll-actual` is capped to $105 worth of collateral (`process-collateral-asset`, lines 818-823).
3. `calc-final-liquidation-amounts` recomputes `debt-final-usd = 105 × 10000/11000 ≈ $95.45` (lines 844-853).
4. Liquidator profit = $105 − $95.45 ≈ $9.55 (≈9.09% of collateral value, not 10% of debt).
5. Now assume collateral crashes further to only $55: `debt-final-usd = 55 × 10000/11000 ≈ $50`, profit ≈ $5 - half the previous absolute profit, even though the liquidator is doing the same amount of protocol-saving work (clearing an underwater position). A liquidator comparing gas/opportunity cost against this shrinking reward will rationally skip liquidating the most severely underwater positions, letting bad debt accumulate for socialization.

### Citations

**File:** docs/market.md (L265-279)
```markdown
### Liquidation Incentives

Liquidators receive a bonus (liquidation penalty) for performing liquidations:

```
Example:
- Borrower owes 100 USDC
- Collateral: 120 USDC worth of zSTX
- Liquidation Penalty: 5%

Liquidator:
- Repays 100 USDC debt
- Receives 105 USDC worth of zSTX
- Net profit: 5 USDC worth of zSTX
```
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

**File:** mainnet/contracts/market/v0-4-market.clar (L818-823)
```text
        (coll-expected (mul-div-down coll-usd-expected (pow u10 coll-decimals) coll-price))
        
        ;; cap at available collateral (user may not have enough)
        (coll-actual (if (> coll-expected user-coll-balance)
                         user-coll-balance
                         coll-expected)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L844-853)
```text
  (let ((coll-actual-usd (normalize (* coll-actual coll-price) coll-decimals false))
        ;; If collateral was capped, recalculate debt proportionally
        (debt-final-usd (if (< coll-actual coll-expected)
                           (calc-liq-debt-repay-real coll-actual-usd liq-penalty)
                           debt-actual-usd))
        (debt-final (mul-div-down debt-final-usd (pow u10 debt-decimals) debt-price)))
    {
      debt-final-usd: debt-final-usd,
      debt-final: debt-final
    }))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1453-1468)
```text
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
    (debt-final (get debt-final final-amounts))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1534-1560)
```text
      ;; Handle bad debt socialization if no collateral left
      (let ((bad-debt-socialized 
              (if no-collateral-left
                  (let ((stripped-debt-list (filter-out-debt-asset (get debt pos-full) debt-aid))
                        (fresh-debt-list (if (is-eq debt-updated u0)
                                             stripped-debt-list
                                             (unwrap-panic (as-max-len?
                                               (append stripped-debt-list
                                                       { aid: debt-aid, scaled: debt-updated })
                                               u64)))))
                    (if (> (len fresh-debt-list) u0) ;; if still has debt
                      (let ((socialization-result (fold socialize-debt-asset 
                                                        fresh-debt-list 
                                                        { borrower: borrower, success: true })))
                        (asserts! (get success socialization-result) ERR-BAD-DEBT-SOCIALIZATION-FAILED)
                        ;; emit bad-debt-socialized event
                        (print {
                          action: "bad-debt-socialized",
                          caller: contract-caller,
                          data: {
                            borrower: borrower,
                            debt-list: fresh-debt-list
                          }
                        })
                        true)
                      false))
                  false)))
```
