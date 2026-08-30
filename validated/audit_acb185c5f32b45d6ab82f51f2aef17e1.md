### Title
Self-liquidation nullifies the liquidation penalty, letting an underwater borrower buy back their own collateral at the liquidation discount while shifting the resulting bad debt onto lenders - (File: `mainnet/contracts/market/v0-4-market.clar`)

### Summary
`liquidate()` never checks that the caller is distinct from the `borrower` being liquidated. This mirrors the UMA H01 pattern: a punitive bond/penalty transfer that is supposed to move value from the "wrong" party to a vindicated counter-party has no real economic effect when the two parties are the same address. Here, the liquidation penalty (collateral bonus) is supposed to move value from the unhealthy borrower to an independent liquidator who performs the risk-reducing service. When `borrower == liquidator`, the borrower simply pays themselves the discounted debt and reclaims their own collateral at the liquidation-penalty discount, capturing the incentive meant for third parties, while any shortfall is still socialized onto the lending pool.

### Finding Description
`liquidate` computes `liquidator` as `contract-caller` [1](#0-0)  and only asserts `contract-caller == tx-sender` and that liquidation is not paused/healthy — there is no assertion preventing `liquidator` from being equal to `borrower` [2](#0-1) .

The collateral bonus (`liq-penalty`) is computed purely from the position's LTV and paid out of the borrower's own collateral to whoever calls `liquidate`, defaulting the receiver to the liquidator: `(actual-receiver (match collateral-receiver recv recv liquidator))` [3](#0-2) . When the position is deep enough in the full-liquidation zone that collateral is insufficient to cover debt + penalty, the code caps `coll-final` at the user's entire remaining balance and computes the shortfall (`remaining-debt-to-repay`) using the maximum penalty discount rate [4](#0-3) . If no collateral remains, the shortfall debt is written off via `socialize-debt-asset`, which reduces the vault's `lindex`, socializing the loss across all depositors [5](#0-4)  and [6](#0-5) .

Because the borrower can call `liquidate` on their own position, they can repay `debt-to-repay` (real tokens transferred into the vault via `vault-system-repay`) and receive back their *entire* remaining collateral balance, which is valued at `debt-to-repay * (1 + liq-penalty-max)` — i.e., they buy back their own collateral at up to a 10% discount using the exact mechanism intended to reward an independent liquidator for absorbing risk. The uncollateralized shortfall is still socialized to lenders exactly as it would be under third-party liquidation, but the discount that is supposed to go to the party performing the liquidation service is instead captured by the very borrower who created the bad debt, with none of the intended deterrent effect.

### Impact Explanation
This nullifies the liquidation-penalty deterrent that is supposed to discourage borrowers from letting positions become insolvent (an underwater borrower has no disincentive — they simply self-liquidate and reclaim their own collateral at a discount rather than losing it to a third party) and converts what should be recovered value for the pool (discount collateral going to a liquidator who took on risk) into value retained by the position owner while lenders still absorb the socialized shortfall. This is protocol insolvency risk: lenders bear write-offs that the liquidation-incentive design is meant to minimize by rewarding rapid, competitive third-party liquidation, and the borrower who caused the bad debt is made whole at the pool's expense.

### Likelihood Explanation
No privileged access is required — any account can call `liquidate` naming itself as both `borrower` and (implicitly, via `contract-caller`) the `liquidator`, and can set `collateral-receiver` or rely on the default. The only constraint is that the position must already be in the liquidation zone (LTV ≥ `LTV-LIQ-PARTIAL`), which the borrower fully controls by their own borrow/withdraw actions or simply by waiting for adverse price movement, and self-liquidating before any competing liquidator's transaction lands.

### Recommendation
Add an explicit check in `liquidate` that `borrower != contract-caller` (and, if `collateral-receiver` is provided, that it is not the borrower or a borrower-controlled address), so that the party being liquidated cannot also be the party receiving the liquidation bonus. Alternatively, route the shortfall/bad-debt write-off logic so that self-initiated liquidations forfeit the penalty bonus (e.g., burn it or route it to the vault) rather than paying it to the borrower.

### Proof of Concept
1. Alice deposits collateral and borrows against it until, due to price movement, her position enters the full-liquidation zone (`LTV ≥ LTV-LIQ-FULL`), such that `coll-final-raw` from `scale-debt-for-liquidation` would leave `coll-remaining > 0`, i.e., her collateral is insufficient to cover debt at `liq-penalty-max`.
2. Instead of waiting for a third party, Alice herself calls `liquidate(borrower=Alice, collateral-ft, debt-ft, debt-amount, min-collateral-expected, none, none)` from her own account.
3. `vault-system-repay` pulls `debt-to-repay` tokens from Alice and credits the vault; `collateral-remove` sends `coll-final` (her full remaining collateral, valued at `debt-to-repay * (1 + liq-penalty-max)`) back to Alice as `actual-receiver` defaults to `liquidator` = Alice [7](#0-6) .
4. `no-collateral-left` evaluates true, and the shortfall debt is socialized to the vault's other depositors via `socialize-debt-asset` / `vault-socialize-debt` [5](#0-4) .
5. Net result: Alice recovers her own collateral at the liquidation discount instead of losing it to an independent liquidator, while lenders in the debt vault absorb the write-off exactly as if a legitimate liquidation had occurred — the intended punitive/incentive transfer has been captured by the party it was meant to penalize.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L1390-1393)
```text
  (let (
    (feeds-check (try! (write-feeds price-feeds)))
    (liquidator contract-caller)
    (position (try! (get-liquidation-position borrower)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1476-1486)
```text
    (coll-remaining (- user-coll-balance coll-final-raw))
    (remaining-debt-to-repay
      (if (> coll-remaining u0)
        (let ((rem-coll-usd (normalize (* coll-remaining coll-price) coll-decimals false))
              (rem-debt-usd (div-bps-down rem-coll-usd (+ BPS liq-penalty-max)))
              (rem-debt-tokens (mul-div-down rem-debt-usd (pow u10 debt-decimals) debt-price))
              (rem-borrow-index (get index (unwrap-panic (get-cached-indexes debt-aid))))
              (rem-scaled (mul-div-down rem-debt-tokens INDEX-PRECISION rem-borrow-index)))
          (mul-div-up rem-scaled rem-borrow-index INDEX-PRECISION))
        u1))
    (coll-final (if (is-eq remaining-debt-to-repay u0) user-coll-balance coll-final-raw)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1488-1493)
```text
    (asserts! (not (is-liquidation-paused debt-aid)) ERR-LIQUIDATION-PAUSED)
    (asserts! (is-eq contract-caller tx-sender) ERR-AUTHORIZATION)
    (asserts! (> debt-amount u0) ERR-AMOUNT-ZERO)
    (asserts! (> debt-to-repay u0) ERR-ZERO-LIQUIDATION-AMOUNTS)
    (asserts! (> coll-final u0) ERR-ZERO-LIQUIDATION-AMOUNTS)
    (asserts! (>= coll-final min-collateral-expected) ERR-SLIPPAGE)
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1495-1512)
```text
    ;; execute liquidation
    (try! (vault-system-repay debt-aid debt-to-repay debt-ft debt-address))

    ;; update obligations and socialize bad debt
    (let ((debt-updated (try! (contract-call? .v0-market-vault
                              debt-remove-scaled
                              borrower
                              scaled-to-remove
                              debt-aid)))
          ;; Collateral receiver defaults to liquidator if not specified
          (actual-receiver (match collateral-receiver recv recv liquidator))
          (coll-removed (try! (contract-call? .v0-market-vault
                              collateral-remove
                              borrower
                              coll-final
                              collateral-ft
                              coll-aid
                              actual-receiver)))
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

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L942-968)
```text
(define-public (socialize-debt (scaled-amount uint))
  (let ((scaled-principal (var-get principal-scaled))
        (borrowed (var-get total-borrowed))
        (idx (var-get index))
        (current-assets (var-get assets))
        (current-lindex (var-get lindex))
        (old-total-assets (total-assets))
        (debt-reduction (mul-div-down scaled-amount idx INDEX-PRECISION))
        (principal-reduction (if (> scaled-principal u0)
                                (mul-div-down scaled-amount borrowed scaled-principal)
                                u0))
        ;; Write down lindex proportionally to loss in total-assets
        (new-lindex (if (and (> old-total-assets u0) (> old-total-assets debt-reduction))
                       (mul-div-down current-lindex (- old-total-assets debt-reduction) old-total-assets)
                       u0)))

    (try! (check-caller-auth))
    (asserts! (> scaled-amount u0) ERR-AMOUNT-ZERO)

    (var-set lindex new-lindex)
    (var-set principal-scaled (if (> scaled-principal scaled-amount) (- scaled-principal scaled-amount) u0))
    (var-set total-borrowed (if (> borrowed principal-reduction) (- borrowed principal-reduction) u0))
    (var-set assets (if (> current-assets principal-reduction) (- current-assets principal-reduction) u0))

    (print {
      action: "socialize-debt",
      caller: contract-caller,
```
