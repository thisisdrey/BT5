## #Vulnerability found for this question

### Title
`socialize-debt` skips interest accrual before writing down `total-borrowed`/`assets`/`index`, desyncing vault debt accounting - (File: `mainnet/contracts/vault/v0-vault-sbtc.clar` and sibling vaults)

### Summary
Every other state-mutating entry point in the vault contracts (`system-borrow`, `system-repay`, `flashloan`, and the deposit/redeem paths) begins by calling `(try! (accrue))` to roll `index`/`lindex`/`assets`/`last-update` forward to the current block before doing any further math. `socialize-debt` is the sole exception: it reads `principal-scaled`, `total-borrowed`, `index`, `assets`, and `lindex` directly from storage without accruing first, then uses those **stale, pre-accrual** values to compute the debt/asset write-down that is committed to storage.

### Finding Description
`socialize-debt` is defined as: [1](#0-0) 

Compare this to `system-borrow`/`system-repay`/`flashloan`, which all invoke `(u (try! (accrue)))` as their very first binding before reading `principal-scaled`, `index`, or `total-borrowed`: [2](#0-1) [3](#0-2) 

The `accrue` function itself computes the new `index`/`lindex`, mints treasury LP for the fee-reserve portion of accrued interest, and only then advances `last-update`: [4](#0-3) 

Because `socialize-debt` never calls `accrue`, its `idx`, `principal-scaled`, `borrowed`, and `old-total-assets` bindings reflect the state as of the *last* accrual, not the true current debt (which has grown by the interest accrued since then). It then converts the caller-supplied `scaled-amount` (representing real, currently-outstanding bad debt) into `debt-reduction`/`principal-reduction` using this stale `idx`, and writes the result directly into `total-borrowed`, `assets`, and `lindex`: [5](#0-4) 

This is the same bug class as the reported `TroveManager.openTrove` issue: a value that should have been derived *after* an accrual step is instead computed from a pre-accrual snapshot, and that stale computation overwrites/updates the aggregate accounting state. Here, the vault's `total-borrowed`/`assets`/`lindex` become permanently desynchronized from the real outstanding debt and real backing assets, because the interest accrued between the last `accrue()` call and the `socialize-debt` call is silently dropped from the write-down math (it is neither added to `assets`/`total-borrowed` first, nor subtracted correctly against the true post-accrual figures).

### Impact Explanation
Because `total-borrowed` and `assets` no longer reflect the true state, downstream computations that depend on them (`total-debt`, `total-assets`, `convert-to-shares`/`convert-to-assets` used for `zft` share pricing, and `CAP-DEBT` checks in `system-borrow`) become inaccurate. Depending on the direction of drift, this can (a) permanently freeze/misallocate depositor funds by mispricing `zft` shares against `total-assets`, or (b) push the vault into an insolvent state where the last depositors cannot redeem because internal `assets`/`total-borrowed` bookkeeping no longer matches the sum of real claims — mirroring the underflow/inability-to-close-Trove failure mode described in the source report. This lands in the in-scope "protocol insolvency / permanent freezing of funds" impact category.

### Likelihood Explanation
`socialize-debt` is gated by `check-caller-auth` (a privileged/market caller, not an arbitrary EOA), so it is not directly callable by any principal — it is invoked as part of the protocol's bad-debt write-off flow (e.g., triggered from liquidation logic reachable by ordinary principals through the market contract). Any liquidation event that results in socialized debt, occurring after even a small amount of time has elapsed since the last `accrue()` call on that vault, will trigger this discrepancy, making it likely to occur under normal operation rather than requiring a contrived scenario.

### Recommendation
Add `(try! (accrue))` as the first step of `socialize-debt`, mirroring `system-borrow`/`system-repay`/`flashloan`, and re-derive `idx`, `principal-scaled`, `borrowed`, and `old-total-assets` from the post-accrual state before computing `debt-reduction`/`principal-reduction`, so the write-down is consistent with the vault's true current debt/asset totals. Apply the same fix to every vault contract that defines `socialize-debt` (`v0-vault-usdc.clar`, `v0-vault-usdh.clar`, `v0-vault-stx.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`), all of which share this identical code path.

### Proof of Concept
1. A vault (e.g., `v0-vault-sbtc.clar`) accrues interest continuously via `next-index`/`next-liquidity-index`; `index`/`lindex`/`assets` are only updated on-chain when `accrue` runs, which happens inside `system-borrow`, `system-repay`, `flashloan`, or the deposit/redeem entry points.
2. Time passes with no such call occurring on the vault (plausible if borrow/repay/deposit/redeem activity is quiet), so real outstanding debt (per `total-debt`, i.e., `principal-scaled * next-index`) has grown beyond the stored `index`-based `total-borrowed`.
3. A liquidation occurs and the market authorises a call to `socialize-debt` with `scaled-amount` corresponding to the real (post-accrual) bad debt being written off.
4. `socialize-debt` computes `debt-reduction`/`principal-reduction` using the stale `idx`/`borrowed` read directly from storage (no `accrue` call), understating the interest portion of the write-down.
5. `total-borrowed`, `assets`, and `lindex` are set to values that do not reflect the interest that had already silently accrued for the socialized principal, permanently desynchronizing the vault's aggregate accounting from the real value of assets/shares outstanding — degrading share pricing (`convert-to-assets`/`convert-to-shares`) for all remaining depositors going forward.

### Citations

**File:** mainnet/contracts/vault/v0-vault-sbtc.clar (L841-877)
```text
          (let ((next (next-index))
                (nliq (next-liquidity-index))
                (scaled-principal (var-get principal-scaled))
                (old-debt (mul-div-down scaled-principal idx INDEX-PRECISION))
                (new-debt (mul-div-down scaled-principal next INDEX-PRECISION))
                (debt-delta (if (> new-debt old-debt) (- new-debt old-debt) u0))
                (reserve-inc (mul-div-down debt-delta (var-get fee-reserve) BPS))
                (treasury-lp (if (> reserve-inc u0) (mul-div-down reserve-inc (total-supply) (- (total-assets-preview) reserve-inc)) u0)))
            (if (not (is-eq idx next))
                (var-set index next)
                false)
            (if (not (is-eq lidx nliq))
                (var-set lindex nliq)
                false)
            (if (> treasury-lp u0)
                (try! (ft-mint? zft treasury-lp .dao-treasury))
                false)
            (if (or (not (is-eq idx next)) (not (is-eq lidx nliq)))
                (var-set last-update stacks-block-time)
                false)
            (ok { index: next, lindex: nliq })))))

(define-public (system-borrow (amount uint) (receiver principal))
  (let (
      (states (var-get pause-states))
      (u (try! (accrue)))
      (CAP-DEBT (var-get cap-debt))
      (available-assets (get-available-assets))
      (scaled-principal (var-get principal-scaled))
      (idx (var-get index))
      (debt (total-debt))
      (scaled-amount (mul-div-up amount INDEX-PRECISION idx))
      (updated-scaled-principal (+ scaled-principal scaled-amount)))

    (try! (check-caller-auth))
    (asserts! (not (get borrow states)) ERR-PAUSED)
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)
```

**File:** mainnet/contracts/vault/v0-vault-sbtc.clar (L900-914)
```text
(define-public (system-repay (amount uint))
  (let (
        (states (var-get pause-states))
        (u (try! (accrue)))
        (scaled-principal (var-get principal-scaled))
        (idx (var-get index))
        (debt (total-debt))
        (total-borrowed-amount (var-get total-borrowed))
        (capped-amount (if (> amount debt) debt amount))
        (principal-reduction (calc-principal-ratio-reduction capped-amount scaled-principal debt))
        (capped-reduction (if (> principal-reduction scaled-principal) scaled-principal principal-reduction))
        (updated-scaled-principal (- scaled-principal capped-reduction))
        (principal-repaid (mul-div-down capped-amount total-borrowed-amount debt))
        (interest-paid (- capped-amount principal-repaid))
        (total-borrowed-new (if (> total-borrowed-amount principal-repaid) (- total-borrowed-amount principal-repaid) u0)))
```

**File:** mainnet/contracts/vault/v0-vault-sbtc.clar (L942-956)
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
```

**File:** mainnet/contracts/vault/v0-vault-sbtc.clar (L960-968)
```text

    (var-set lindex new-lindex)
    (var-set principal-scaled (if (> scaled-principal scaled-amount) (- scaled-principal scaled-amount) u0))
    (var-set total-borrowed (if (> borrowed principal-reduction) (- borrowed principal-reduction) u0))
    (var-set assets (if (> current-assets principal-reduction) (- current-assets principal-reduction) u0))

    (print {
      action: "socialize-debt",
      caller: contract-caller,
```
