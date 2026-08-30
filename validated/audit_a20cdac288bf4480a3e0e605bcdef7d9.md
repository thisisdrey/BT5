### Title
Liquidation reverts entirely (blocking healthy liquidations) if a single vault's `socialize-debt` or `accrue` call fails for a borrower holding multiple debt assets - (File: `mainnet/contracts/market/v0-4-market.clar`)

### Summary
`liquidate()` in `v0-4-market.clar` repays one debt asset and seizes one collateral asset for a borrower, but when the borrower's collateral is fully exhausted, it must additionally socialize *all* of the borrower's remaining debt across *every* vault the borrower has debt in, within the same atomic transaction. This fold-based, all-or-nothing socialization step means a single broken/paused vault for an unrelated debt asset can revert the entire `liquidate()` call - including the already-computed repay/seize of the healthy asset pair - directly mirroring the Union Finance AssetManager bug class where one broken adapter blocks operations meant for other adapters.

### Finding Description
`liquidate()` first performs the primary repay/seize on the targeted `debt-aid`/`coll-aid` pair via `vault-system-repay` and `market-vault.collateral-remove` [1](#0-0) . If this leaves the borrower with `no-collateral-left`, the function must write off all remaining debt across the borrower's full debt list (`fresh-debt-list`, which can span multiple different asset vaults) by folding `socialize-debt-asset` over it [2](#0-1) .

`socialize-debt-asset` calls `vault-socialize-debt` (routed per-asset to the correct vault) and `vault-accrue` for each debt entry, using `unwrap!` to short-circuit into a `failed-status` accumulator if any single call fails [3](#0-2) . Crucially, after the fold completes, the code asserts the *aggregate* success flag:
```
(asserts! (get success socialization-result) ERR-BAD-DEBT-SOCIALIZATION-FAILED)
``` [4](#0-3) 

Because this `asserts!` sits inside the same public function as the earlier `vault-system-repay` and `market-vault.collateral-remove` calls, a failure on **any single unrelated debt-asset's vault** (e.g., its `socialize-debt` reverts because the vault is paused, or `check-caller-auth`/`ERR-AMOUNT-ZERO` trips for that specific asset, or the vault contract is otherwise broken) causes the whole transaction, including the parts that succeeded for the healthy targeted asset pair, to roll back via Clarity's atomicity. Each `v0-vault-*` contract exposes independent per-operation pause flags (e.g., `borrow`, `repay`, `deposit`, `redeem`) gated via `pause-states`, and `socialize-debt`/`system-repay` calls in these vaults revert on failed `check-caller-auth` or arithmetic issues rather than degrading gracefully [5](#0-4) . This is architecturally identical to the Union Finance `AssetManager` bug: a healthy adapter's deposit/withdraw/rebalance gets reverted purely because a sibling adapter used in the same batched call is broken/paused.

Separately, `accrue-user-debts`/`accrue-user-collateral` (used at the top of `borrow`, `repay`, `collateral-remove`, and `liquidate`) fold over a user's *entire* multi-asset position and call `unwrap-panic (accrue-and-cache (get aid debt-entry))` for every asset the user holds, not just the one being acted on [6](#0-5) . `accrue` in the vaults is designed to pass through without reverting when paused [7](#0-6) , which mitigates that specific path, but it does not protect the bad-debt-socialization path described above, since `socialize-debt` itself is not designed to "pass through" on failure - it hard-fails via `unwrap!`/`check-caller-auth`.

### Impact Explanation
This lands in the **temporary freezing of funds** category. If any single vault (asset) that a to-be-liquidated borrower has debt in becomes paused or otherwise reverts on `socialize-debt`/`accrue`, liquidators cannot liquidate that borrower's fully-undercollateralized position at all, even for the other debt/collateral assets whose vaults are healthy. This can leave insolvent positions unliquidated protocol-wide for as long as the one affected vault remains broken, exposing the protocol and other lenders to accumulating bad debt and delayed loss recognition - a direct funds-freezing/insolvency-risk analog to the referenced Union Finance issue.

### Likelihood Explanation
Requires: (1) a borrower with debt across ≥2 different assets, (2) that borrower's collateral becomes fully exhausted by a liquidation, triggering the bad-debt socialization branch, and (3) one of those debt assets' vaults being paused (an admin/DAO action, not attacker-controlled) or reverting for any other reason. This is a realistic operational scenario (DAO pausing a vault due to an exploit or oracle issue elsewhere) rather than a contrived edge case, though it does depend on the vault-pause admin action occurring at the same time as an at-risk multi-asset position needing full liquidation.

### Recommendation
Make the bad-debt socialization step resilient to individual vault failures instead of asserting on an all-or-nothing aggregate result. For example, use `match`/`try`-style per-item error capture (analogous to `liquidate-multi`'s pattern of not reverting the whole batch on one failure) so that a failure to socialize debt on one asset does not roll back the already-executed repay/seize of the targeted asset pair. Optionally, track partially-socialized debt so it can be retried later once the affected vault is unpaused, rather than blocking the entire liquidation.

### Proof of Concept
1. Borrower has collateral in `vault-usdc` (fully exhausted after this liquidation) and debt in both `vault-usdc` and `vault-usdh`.
2. DAO pauses `vault-usdh`'s `socialize-debt`/underlying auth path (or it reverts due to any other fault) while the position remains unhealthy.
3. Liquidator calls `liquidate()` targeting the `USDC` debt/collateral pair; `vault-system-repay` and `market-vault.collateral-remove` succeed, `no-collateral-left` becomes true.
4. `fold socialize-debt-asset` processes the `fresh-debt-list` containing both `USDC` and `USDH` entries; the `USDH` entry's `vault-socialize-debt` call fails, setting `success: false` in the accumulator.
5. `(asserts! (get success socialization-result) ERR-BAD-DEBT-SOCIALIZATION-FAILED)` fails, reverting the entire `liquidate()` transaction - including the previously-successful `USDC` repay/seize - even though the `USDC` vault was completely healthy.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L259-268)
```text
(define-private (accrue-user-debts (debt-list (list 64 { aid: uint, scaled: uint})))
  (fold accrue-debt-asset debt-list { success: true }))

(define-private (accrue-debt-asset
  (debt-entry { aid: uint, scaled: uint })
  (acc { success: bool }))
  (begin
    ;; this will use cache if available, accrue if not
    (unwrap-panic (accrue-and-cache (get aid debt-entry)))
    acc))
```

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

**File:** mainnet/contracts/market/v0-4-market.clar (L1519-1535)
```text
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

      ;; Handle bad debt socialization if no collateral left
      (let ((bad-debt-socialized 
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1544-1548)
```text
                    (if (> (len fresh-debt-list) u0) ;; if still has debt
                      (let ((socialization-result (fold socialize-debt-asset 
                                                        fresh-debt-list 
                                                        { borrower: borrower, success: true })))
                        (asserts! (get success socialization-result) ERR-BAD-DEBT-SOCIALIZATION-FAILED)
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L833-840)
```text
(define-public (accrue)
  (let ((states (var-get pause-states))
        (idx (var-get index))
        (lidx (var-get lindex)))
      (if (get accrue states)
          ;; PAUSED: Pass-through without reverting
          (ok { index: idx, lindex: lidx })
          ;; NOT PAUSED: Normal accrual logic
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L942-964)
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
```
