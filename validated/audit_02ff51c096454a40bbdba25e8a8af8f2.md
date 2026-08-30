### Title
`socialize-debt` in vault contracts writes down `lindex`/`total-borrowed`/`assets` using a stale, un-accrued `index`/`lindex` before applying the reduction - (File: `mainnet/contracts/vault/v0-vault-sbtc.clar` and sibling vaults `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`, `v0-vault-stx.clar`, `v0-vault-usdc.clar`, `v0-vault-usdh.clar`)

### Summary
Each vault's `socialize-debt` public function reads `index`, `lindex`, `total-borrowed`, `assets`, and `principal-scaled` directly via `var-get` and computes `debt-reduction`, `principal-reduction`, and the new `lindex` from those raw, potentially-stale storage values, without first calling `accrue` to roll the interest index forward to `stacks-block-time`. This is the exact bug class described in the report: functions that mutate accounting state (`lindex`, `total-borrowed`, `assets`) do so using values that predate the last elapsed interest period, so the write-down calculation does not reflect the true, current debt owed by the vault at the time bad debt is socialized.

### Finding Description
Compare `socialize-debt` with every other state-mutating vault entry point (`system-borrow`, `system-repay`, `deposit`, `redeem`, `set-fee-reserve`, `set-points-util`, `set-points-rate`) in the same file, all of which begin with `(u (try! (accrue)))` or `(try! (accrue))` before touching `index`/`lindex`/derived amounts: [1](#0-0) 

`socialize-debt`, however, omits this call entirely: [2](#0-1) 

It uses `(idx (var-get index))` and `(current-lindex (var-get lindex))` directly as the basis for `debt-reduction`, `principal-reduction`, and the new `lindex`: [3](#0-2) 

If time has elapsed since `last-update` (i.e., interest has accrued but not yet been materialized into `index`/`lindex` via `accrue`), then `idx` under-states the true per-scaled-unit debt, and `current-lindex`/`old-total-assets` under-state true vault assets/liabilities. Since `debt-reduction = scaled-amount * idx / INDEX-PRECISION` is computed on the stale `idx`, and the caller (market.clar's liquidation path) determines `scaled-amount` (the full remaining scaled debt of a wiped-out borrower) based on separately-cached, possibly more up-to-date indexes, the write-down applied inside the vault (`lindex`, `total-borrowed`, `assets`) can diverge from the true value that should have been socialized, corrupting `total-assets()`/`lindex` bookkeeping that all future `redeem`/`deposit` share-price conversions rely on.

This is reachable by an ordinary principal: `market.clar::liquidate()` is a permissionless entry point any liquidator can call. When a liquidation empties a borrower's collateral while debt remains, `liquidate()` invokes `socialize-debt-asset`, which calls `vault-socialize-debt` (i.e., the vault's `socialize-debt`) *before* calling `vault-accrue` to refresh the index cache: [4](#0-3) 

So the vault's own internal `index`/`lindex` are stale at the moment `socialize-debt` performs its accounting write-down, exactly mirroring the reported pattern where `Accounting::reduceReserve()` mutated `reserveNav`/`nav` using pre-update values.

### Impact Explanation
Because `socialize-debt` mutates the vault's `lindex` (which directly determines the liquidity-index used to price `zft` shares for depositors) and `total-borrowed`/`assets` using an un-accrued index, the shares-to-assets conversion rate (`convert-to-assets-preview`) for all subsequent depositors/redeemers becomes based on incorrect internal state. Depending on the direction of the divergence, this either permanently freezes/mis-prices a portion of depositor yield (temporary/permanent freezing of unclaimed yield for zft-token holders) or allows depositors to redeem against an inflated `assets`/`lindex` value at the expense of remaining depositors (theft of unclaimed yield from other suppliers) — both fall under the in-scope "theft of unclaimed yield" / "temporary freezing of funds" High-impact classes.

### Likelihood Explanation
Every bad-debt liquidation where a borrower's collateral is fully exhausted while debt remains triggers this path (`no-collateral-left` branch of `liquidate()`), and liquidations are routine, permissionless operations that can occur many blocks after the vault's `last-update`. No special privilege or governance action is required — an ordinary liquidator triggers it simply by liquidating an underwater position that ends up with leftover bad debt, making the divergence practically certain to occur whenever a socialization happens after some elapsed time without an intervening accrual-triggering operation (deposit/redeem/borrow/repay) on that vault.

### Recommendation
Call `(try! (accrue))` at the top of `socialize-debt` in every vault contract, mirroring every other mutating entry point, so that `index`, `lindex`, `total-borrowed`, and `assets` are rolled forward to `stacks-block-time` before the bad-debt write-down is computed and applied. Additionally, in `market.clar::socialize-debt-asset`, ensure the vault's index cache refresh (`vault-accrue`) happens consistently with (ideally prior to) the socialization call, not only after it.

### Proof of Concept
1. Deposit into a vault (e.g., `v0-vault-usdc`) and have a borrower open a debt position against it, then let a meaningful amount of time pass without any deposit/redeem/borrow/repay call on that vault (so `index`/`lindex` remain stale relative to `stacks-block-time`).
2. Cause the borrower's collateral value to drop (oracle price movement) until the position is liquidatable with the collateral fully consumed, leaving residual scaled debt (`no-collateral-left` true in `liquidate`).
3. Call `market.clar::liquidate()` as an ordinary liquidator. This triggers `socialize-debt-asset` → vault `socialize-debt`, which computes `debt-reduction`/new `lindex` using the vault's un-accrued `index`/`lindex` (`var-get index`, `var-get lindex` without a prior `accrue`), rather than the interest-accrued current values.
4. Compare `lindex`/`total-assets()` immediately after this call against what they would be had `accrue` been called first — the divergence (proportional to `interest-rate * time-since-last-update`) demonstrates the mis-accounted write-down, which subsequently skews `convert-to-assets-preview` for every depositor interacting with the vault afterward. [5](#0-4) [4](#0-3)

### Citations

**File:** mainnet/contracts/vault/v0-vault-sbtc.clar (L863-870)
```text
(define-public (system-borrow (amount uint) (receiver principal))
  (let (
      (states (var-get pause-states))
      (u (try! (accrue)))
      (CAP-DEBT (var-get cap-debt))
      (available-assets (get-available-assets))
      (scaled-principal (var-get principal-scaled))
      (idx (var-get index))
```

**File:** local-testing/contracts/vault/vault-sbtc.clar (L946-986)
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
      data: {
        scaled-amount: scaled-amount,
        debt-reduction: debt-reduction,
        principal-reduction: principal-reduction,
        old-lindex: current-lindex,
        new-lindex: new-lindex,
        old-total-assets: old-total-assets,
        principal-scaled: (if (> scaled-principal scaled-amount) (- scaled-principal scaled-amount) u0),
        total-borrowed: (if (> borrowed principal-reduction) (- borrowed principal-reduction) u0),
        index: idx
      }
    })

    (ok true)))
```

**File:** local-testing/contracts/market/market.clar (L901-925)
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
            (unwrap! (contract-call? .market-vault
                                      debt-remove-scaled
                                      borrower
                                      scaled-debt
                                      asset-id) failed-status)
          acc)
        ))
```
