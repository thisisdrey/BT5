### Title
Bad-debt socialization across multiple debt assets can DOS the entire liquidation - ([File: mainnet/contracts/market/v0-4-market.clar])

### Summary
In `liquidate`, when a borrower's collateral is fully exhausted, the contract must socialize any remaining bad debt across **all** of the borrower's debt assets by folding `socialize-debt-asset` over `fresh-debt-list`. If the vault call for any single one of those debt assets fails, the whole `liquidate` transaction reverts, exactly mirroring the Notional rebalance DOS pattern where one failing external interaction blocks an entire multi-asset batch operation.

### Finding Description
`liquidate` builds `fresh-debt-list`, the borrower's outstanding debts across potentially several distinct vaults/assets, and when `no-collateral-left` is true it folds `socialize-debt-asset` over that list: [1](#0-0) 

`socialize-debt-asset` itself is written to short-circuit on prior failure, but each individual step is wrapped in `unwrap!`, so a failure on any one asset in the list flips `success` to `false`: [2](#0-1) 

After the fold completes, the code asserts the aggregate `success` flag; if any single asset's socialization failed, this `asserts!` reverts the entire `liquidate` call with `ERR-BAD-DEBT-SOCIALIZATION-FAILED`, undoing the collateral seizure and debt repayment that had already been computed for the primary collateral/debt pair being liquidated in this same call: [3](#0-2) 

This is structurally identical to the Notional bug: a loop that must succeed on **every** element (every external vault call in Notional's case, every debt-asset vault call here) will cause the entire outer operation to fail if even one element reverts. Each iteration performs three separate cross-contract/cross-vault operations — `vault-socialize-debt`, `vault-accrue`, and `.v0-market-vault debt-remove-scaled` — any of which reverting (e.g. because that vault is paused for the relevant operation, has a stale/failing index cache, or an underflow/edge-case in its internal accounting) aborts the batch for all other, unrelated debt assets in the position, not just the failing one.

### Impact Explanation
If a borrower with debt spread across multiple assets becomes fully insolvent (no collateral left) and one of those debt assets' vaults cannot successfully process `socialize-debt`/`accrue`/`debt-remove-scaled` (e.g., that vault is paused, or hits an unexpected revert condition), liquidators are permanently unable to liquidate that position at all — the `liquidate` call always reverts at the `ERR-BAD-DEBT-SOCIALIZATION-FAILED` assertion. This leaves genuinely insolvent debt on the books indefinitely, blocking bad-debt socialization and freezing the ability to close out or write down that position, which constitutes temporary/permanent freezing of protocol funds (the collateral that should have been seized, and the ability to write off unrecoverable debt) — falling under the in-scope "temporary freezing of funds" / insolvency-adjacent impact category, and directly analogous to the accepted Notional M-7 finding.

### Likelihood Explanation
This requires a borrower to hold debt in more than one asset and become fully undercollateralized (an ordinary, reachable state for any borrower using multi-asset borrowing), combined with one of those debt vaults being in a state where its `socialize-debt`/`accrue`/`debt-remove-scaled` path reverts. This is not a privileged-DAO-manipulation scenario — pausing individual vault operations is a normal operational lever, and any of the vaults' pause flags or internal edge cases being active at the time of a needed liquidation is a realistic operational condition, especially since it only takes one out of potentially many debt assets in the list to trigger the whole-transaction revert.

### Recommendation
Make bad-debt socialization resilient to per-asset failures, mirroring the recommended fix for the Notional analog: instead of asserting on the aggregate `success` flag and reverting the whole `liquidate` call, socialize whatever debt assets can be successfully processed, and either (a) retry/queue the failed asset separately, or (b) allow the liquidation of the primary collateral/debt pair to complete even if socialization of an unrelated debt asset fails, with the failure surfaced via event/log rather than a hard revert of the entire transaction.

### Proof of Concept
1. Borrower opens positions with debt in Asset A (vault V_A) and Asset B (vault V_B), and collateral in Asset C.
2. Collateral price drops so that liquidating against Asset A collateral would leave `no-collateral-left = true`, triggering bad-debt socialization for both A and B via `fresh-debt-list` (`mainnet/contracts/market/v0-4-market.clar:1537-1544`).
3. Vault V_B has its relevant pause flag set (or any other condition causing its `socialize-debt`, `accrue`, or `debt-remove-scaled` call to fail).
4. A liquidator calls `liquidate` for the Asset A debt/collateral pair; `socialize-debt-asset` iterates to Asset B, the vault call fails, `success` becomes `false`.
5. `(asserts! (get success socialization-result) ERR-BAD-DEBT-SOCIALIZATION-FAILED)` at `mainnet/contracts/market/v0-4-market.clar:1548` reverts the entire transaction, so even the fully valid liquidation of the Asset A pair with Asset C collateral cannot proceed, and this remains true for every future liquidation attempt on this borrower until V_B's issue is resolved.

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

**File:** mainnet/contracts/market/v0-4-market.clar (L1544-1549)
```text
                    (if (> (len fresh-debt-list) u0) ;; if still has debt
                      (let ((socialization-result (fold socialize-debt-asset 
                                                        fresh-debt-list 
                                                        { borrower: borrower, success: true })))
                        (asserts! (get success socialization-result) ERR-BAD-DEBT-SOCIALIZATION-FAILED)
                        ;; emit bad-debt-socialized event
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1558-1572)
```text
                        true)
                      false))
                  false)))
        
        ;; emit main liquidate event
        (print {
          action: "liquidate",
          caller: contract-caller,
          data: {
            liquidator: liquidator,
            borrower: borrower,
            collateral-asset-id: coll-aid,
            collateral-asset-addr: coll-address,
            debt-asset-id: debt-aid,
            debt-asset-addr: debt-address,
```
