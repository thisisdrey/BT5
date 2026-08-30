## Title
`liquidate-multi` swallows individual liquidation failures after collateral/debt state is already committed, leaving unsocialized bad debt permanently stuck - (File: `mainnet/contracts/market/v0-4-market.clar`)

### Summary
`liquidate-multi` iterates over a batch of liquidations using a plain `map`, explicitly by design not propagating a failing individual liquidation's error to the top level: [1](#0-0) 

Each element calls `liquidate` as a normal in-contract function call (not via `contract-call?`): [2](#0-1) 

Because `liquidate` performs several successful `contract-call?`s to `.v0-market-vault` and the vault (repay, `debt-remove-scaled`, `collateral-remove`) *before* it reaches the final bad-debt-socialization step, and only fails afterwards via `asserts!`, the earlier state-changing sub-calls remain committed to chain state (their own `contract-call?` sub-transaction succeeded) even though `liquidate` ultimately returns an error. Since `liquidate-multi` never uses `try!`/`unwrap!` on the result of `call-liquidate`, the *overall* top-level transaction still returns `(ok ...)`, so nothing about the whole transaction gets rolled back. This is functionally the same bug class as the ZetaChain report: partial execution is committed, the operation is reported as failed ("Aborted" analog), and there is no automatic mechanism to reconcile or refund the resulting inconsistent state.

### Finding Description
`liquidate` computes and applies collateral seizure and debt reduction, then — only if the position ends up with `no-collateral-left` — attempts to socialize any remaining (now-uncollateralized) debt via a `fold` over `socialize-debt-asset`: [3](#0-2) [4](#0-3) 

`socialize-debt-asset` uses `unwrap!` (not `try!`) internally, so if `vault-socialize-debt` fails for one asset, the fold degrades to a `success: false` accumulator without aborting the enclosing function: [5](#0-4) 

Back in `liquidate`, `asserts! (get success socialization-result) ERR-BAD-DEBT-SOCIALIZATION-FAILED` then fails the *individual* `liquidate` invocation. Within a single call to `liquidate`, by itself this failure would revert the whole transaction (Clarity's docs state operations are meant to be atomic — "Either full success or full revert — No partial state updates", per `docs/market.md`). However, `liquidate-multi` deliberately breaks this guarantee by calling `liquidate` through a bare, unwrapped, in-contract function call inside `map`, explicitly documented as: "Failed liquidations return error codes but don't revert entire batch." Because the top-level transaction (the call to `liquidate-multi`) still returns `(ok (list ...))` overall, none of the successful `contract-call?`s made earlier inside the failing `liquidate` invocation (debt repay, `debt-remove-scaled`, `collateral-remove` which already sent the borrower's collateral to the liquidator) are unwound.

The net effect: a borrower's collateral can be fully seized and their tracked debt partially reduced, while the bad-debt write-off for the remaining unbacked debt never completes — leaving dangling, uncollateralized debt in the position that the normal liquidation path can no longer clean up (since it would fail the same way again), and that is never reflected as socialized loss in the vault's `lindex`/`assets` accounting.

### Impact Explanation
This produces protocol insolvency: debt is recorded as outstanding against a position with no collateral to back it, and the corresponding vault-side write-down (`socialize-debt`, which reduces `assets`/`total-borrowed` and marks the loss) never executes for that debt, since it failed inside the batch and the failure was swallowed. This inflates on-chain accounted vault assets versus real backing, matching the Critical impact category of "protocol insolvency."

### Likelihood Explanation
Any user (a liquidator using `liquidate-multi`) can trigger this simply by including a liquidatable position whose bad-debt socialization step is engineered or happens to fail (e.g., `vault-socialize-debt` under specific state, or any transient failure), alongside other positions in the same batch call. No privileged access or DAO compromise is required — this is a normal liquidator-invoked market entry point.

### Recommendation
`liquidate-multi` should not use a bare `map` over `call-liquidate` if the intent is genuinely atomic-per-position liquidation; each iteration must either (a) fully commit or fully revert its own sub-state (which Clarity does support at `contract-call?` boundaries — i.e., invoke `liquidate` via `contract-call?` to itself so a failure rolls back just that position's changes), or (b) if partial success truly is intended, add an explicit fallback in `liquidate` itself so that when bad-debt socialization fails, the debt-removal and collateral-removal already performed are not left as a stranded inconsistent state (e.g., ensure the debt list update and socialization happen atomically together, or revert the whole `liquidate` call via `contract-call?` isolation rather than plain in-contract calls).

### Proof of Concept
1. Prepare a borrower position that is liquidatable and results in `no-collateral-left = true` after the liquidation amounts are applied (i.e., only one collateral asset left, or all other collateral fully exhausted).
2. Craft/trigger a scenario where `vault-socialize-debt` (called from `socialize-debt-asset`) fails for the borrower's remaining debt asset (e.g., an internal precondition in the vault's `socialize-debt` fails for the given `scaled-amount`, such as `ERR-AMOUNT-ZERO` edge case or another vault-level assertion).
3. Call `liquidate-multi` with a list containing this position (optionally batched with other valid liquidations).
4. Observe: `call-liquidate` returns an `err` entry for this position, but `liquidate-multi` still returns `(ok (list ... err ...))` overall — meaning the transaction succeeds.
5. Inspect on-chain state: the borrower's collateral has been transferred to the liquidator via the committed `collateral-remove` call and its debt reduced via `debt-remove-scaled`, yet the bad-debt socialization (vault `assets`/`lindex` write-down) never occurred, leaving unbacked debt/accounting inconsistency with no way to reconcile through the normal liquidation flow.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L879-903)
```text
(define-private (socialize-debt-asset
                (debt-entry { aid: uint, scaled: uint })
                (acc { borrower: principal, success: bool }))
  ;; Early return if previous socialization failed
  (if (not (get success acc))
      acc
      (let ((borrower (get borrower acc))
            (failed-status { borrower: borrower, success: false })
            (asset-id (get aid debt-entry))
            (scaled-debt (get scaled debt-entry)))

            ;; Socialize in vault - pass scaled directly to avoid rounding
            (unwrap! (vault-socialize-debt asset-id scaled-debt) failed-status)
            ;; Refresh cache with new indexes post-write-down (lindex decreased)
            (map-set index-cache
                     { timestamp: stacks-block-time, aid: asset-id }
                     (unwrap! (vault-accrue asset-id) failed-status))
            ;; Remove from obligation
            (unwrap! (contract-call? .v0-market-vault
                                      debt-remove-scaled
                                      borrower
                                      scaled-debt
                                      asset-id) failed-status)
          acc)
        ))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L905-918)
```text
;; -- Liquidation: batch helper ----------------------------------------------

(define-private (call-liquidate (position { borrower: principal,
                                            collateral-ft: <ft-trait>,
                                            debt-ft: <ft-trait>,
                                            debt-amount: uint,
                                            min-collateral-expected: uint }))
  (liquidate (get borrower position)
             (get collateral-ft position)
             (get debt-ft position)
             (get debt-amount position)
             (get min-collateral-expected position)
             none   ;; collateral-receiver defaults to liquidator
             none)) ;; price-feeds not supported in batch - update prices separately
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1495-1533)
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

          (target-coll-full-usd (normalize (* user-coll-balance coll-price) coll-decimals false))
          (other-coll-usd (if (> total-collateral-usd target-coll-full-usd)
                              (- total-collateral-usd target-coll-full-usd)
                              u0))
          (other-debt-repayable
            (if (> other-coll-usd u0)
              (let ((other-adj (div-bps-down other-coll-usd (+ BPS liq-penalty-max)))
                    (other-tokens (mul-div-down other-adj (pow u10 debt-decimals) debt-price))
                    (other-borrow-idx (get index (unwrap-panic (get-cached-indexes debt-aid))))
                    (other-scaled (mul-div-down other-tokens INDEX-PRECISION other-borrow-idx)))
                (mul-div-up other-scaled other-borrow-idx INDEX-PRECISION))
              u0))
          (no-collateral-left (and
                                (is-eq coll-removed u0)
                                (or
                                  (is-eq (len (get collateral pos-full)) u1)
                                  (and
                                    (is-eq (len (get collateral pos-full)) (len (get collateral position)))
                                    (is-eq other-debt-repayable u0))))))

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
