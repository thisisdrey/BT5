### Title
Protocol interest fee is permanently stuck when a vault's total-supply is zero during interest accrual - (File: `mainnet/contracts/vault/v0-vault-usdc.clar`)

### Summary
In the Zest lending vaults (`v0-vault-usdc.clar`, `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`, `v0-vault-usdh.clar`, `v0-vault-stx.clar`), `accrue()` skims a protocol fee out of newly accrued interest and mints it as LP shares (`treasury-lp`) to `.dao-treasury`. The fee-share calculation is scaled by `total-supply`, so when `total-supply` is `0` — e.g. the last depositor has fully redeemed while a borrower's debt is still outstanding and continuing to accrue interest — the computed `treasury-lp` collapses to `0` and the protocol's cut of that interest is never minted to anyone, exactly mirroring the referenced report's root cause ("no bond supply … to distribute the interest to").

### Finding Description
`accrue()` computes the reserve fee for a period and converts it into LP shares proportional to `total-supply`: [1](#0-0) 

The share conversion formula is:
```
treasury-lp = mul-div-down reserve-inc total-supply (total-assets-preview - reserve-inc)
```
which is multiplied by `total-supply` and therefore is forced to `0` whenever `total-supply` is `0`, regardless of how large `reserve-inc` (the fee's dollar value) actually is [2](#0-1) . The identical pattern (`calc-treasury-lp-preview` / `accrue`) is duplicated across every vault contract, including `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`, and `v0-vault-usdh.clar` [3](#0-2) .

`total-supply` is the sum of outstanding zft shares held by depositors, and nothing in `deposit`/`redeem` enforces a non-zero floor: `redeem()` allows a user to burn their entire balance down to zero shares as long as there is enough liquidity, with no minimum-liquidity lock preventing the pool from reaching `total-supply == 0` [4](#0-3) . Meanwhile, outstanding debt (`principal-scaled`/`total-borrowed`) is tracked independently of `total-supply` — a borrower can still owe debt to the vault, and that debt continues to accrue interest via `next-index`/`next-liquidity-index` even if every depositor has redeemed their shares [5](#0-4) .

In that state, each subsequent `accrue()` call correctly grows `debt` (via `total-debt`/`debt-preview`) and correctly computes a nonzero `reserve-inc`, but the treasury-lp minting term evaluates to `0` because it is scaled by the (zero) `total-supply`. The protocol's fee share of that interest is never minted to `.dao-treasury` and is not represented by any liquidity token, so it cannot ever be claimed — it is not carried forward through `total-assets`/`total-supply` bookkeeping in a way any principal can later redeem, since the very next depositor's shares are minted 1:1 to their deposited amount irrespective of pre-existing residual value: [6](#0-5) 

This is the direct analog of the referenced report: the code performs an interest/fee distribution step (`accrue`'s `treasury-lp` mint) without checking whether there is any outstanding supply/holder to receive it, so the fee is silently and permanently lost whenever the triggering condition (zero total-supply with outstanding debt) occurs.

### Impact Explanation
This is unclaimed-yield loss to the protocol treasury (`.dao-treasury`), which is the entity entitled to the reserve fee portion of interest. Every accrual period that occurs while `total-supply == 0` and debt is outstanding permanently forfeits that period's protocol fee, with no recovery mechanism — it cannot be reissued later because `accrue()` does not track/carry over an unminted fee balance. This matches the in-scope High impact category: permanent freezing (loss) of unclaimed yield.

### Likelihood Explanation
Requires ordinary, unprivileged usage: any market where a vault has a single dominant depositor lends assets out to borrowers and then fully redeems, while the borrower's loan remains outstanding and continues to accrue interest across subsequent blocks before the vault gets new deposits. No privileged action or DAO compromise is needed; it is a reachable sequence of normal `deposit`, `system-borrow`, and `redeem` calls plus the passage of time triggering `accrue()`.

### Recommendation
In `accrue()`, when `total-supply` is `0` and `reserve-inc > 0`, either (a) accumulate the unminted fee in a separate persisted variable to be minted once a nonzero supply exists again, or (b) enforce a non-zero minimum total-supply floor (the existing but apparently unused `MINIMUM-LIQUIDITY` constant suggests this was intended) so `redeem()` can never fully drain shares to zero while debt is outstanding.

### Proof of Concept
1. A single depositor deposits into `v0-vault-usdc`, minting shares 1:1 (`total-supply > 0`).
2. Vault lends the deposited assets to a borrower via `system-borrow`, so `total-borrowed`/`principal-scaled` become nonzero.
3. The depositor calls `redeem` for their full share balance; `total-supply` becomes `0` while debt remains outstanding.
4. Time passes; a call that triggers `accrue()` runs (e.g. another user's unrelated `system-borrow`, `system-repay`, or `deposit` from a different vault interacting through market logic that calls `accrue`).
5. `reserve-inc` is computed as nonzero from the accrued interest, but `treasury-lp = mul-div-down reserve-inc 0 (...) = 0`, so no shares are minted to `.dao-treasury`: [7](#0-6) .
6. That period's protocol fee is permanently unrecoverable.

### Citations

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L308-315)
```text
        (ts (total-supply-preview)))
    (if (is-eq ts u0)
        amount
        (if (is-eq ta u0)
            u0
            (mul-div-down amount ts ta)))))

(define-private (convert-to-assets-preview (amount uint))
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L352-363)
```text
        (old-debt (mul-div-down scaled-principal idx INDEX-PRECISION))
        (new-debt (mul-div-down scaled-principal next INDEX-PRECISION))
        (debt-delta (if (> new-debt old-debt) (- new-debt old-debt) u0))
        (reserve-inc (mul-div-down debt-delta (var-get fee-reserve) BPS))
        (ta-preview (total-assets-preview)))
    (if (> reserve-inc u0)
        (mul-div-down reserve-inc (total-supply) (- ta-preview reserve-inc))
        u0)))

(define-private (total-supply-preview)
  (let ((current-supply (total-supply))
        (treasury-lp (calc-treasury-lp-preview)))
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L381-409)
```text
        idx
        (let (
            (rate (interest-rate))
            (time-delta (- stacks-block-time (var-get last-update)))
            (multiplier (if (is-eq time-delta u0)
                          INDEX-PRECISION
                          (calc-multiplier-delta rate time-delta true))))
          (calc-index-next idx multiplier)))))

(define-private (next-liquidity-index)
  (let ((states (var-get pause-states))
        (lidx (var-get lindex)))
    (if (get accrue states)
        lidx
        (let (
            (rate (interest-rate))
            (liquidity-rate (calc-liquidity-rate rate (utilization) (var-get fee-reserve)))
            (time-delta (- stacks-block-time (var-get last-update)))
            (multiplier (if (is-eq time-delta u0)
                          INDEX-PRECISION
                          (calc-multiplier-delta liquidity-rate time-delta false))))
          (calc-index-next lidx multiplier)))))

(define-private (principal-ratio-reduction (amount uint))
  (calc-principal-ratio-reduction amount (var-get principal-scaled) (debt-preview)))

;; -- Permission helpers -----------------------------------------------------

(define-private (set-permission-single 
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L795-815)
```text
(define-public (redeem (amount uint) (min-out uint) (recipient principal))
  (let (
    (states (var-get pause-states))
    (u (try! (accrue)))
    (account contract-caller)
    (current-assets (var-get assets))
    (balance (get-balance-internal account))
    (balance-check (asserts! (>= balance amount) ERR-INSUFFICIENT-BALANCE))
    (available-assets (get-available-assets))
    (inkind (convert-to-assets-preview amount)))

  (asserts! (>= current-assets inkind) ERR-INSUFFICIENT-ASSETS)
  (asserts! (not (get redeem states)) ERR-PAUSED)
  (asserts! (> amount u0) ERR-AMOUNT-ZERO)
  (asserts! (> inkind u0) ERR-OUTPUT-ZERO)
  (asserts! (>= inkind min-out) ERR-SLIPPAGE)
  (asserts! (>= available-assets inkind) ERR-INSUFFICIENT-LIQUIDITY)

  (try! (ft-burn? zft amount account))
  (try! (send-underlying inkind recipient))
  (var-set assets (- current-assets inkind))
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L841-861)
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
```

**File:** mainnet/contracts/vault/v0-vault-sbtc.clar (L348-359)
```text
(define-private (calc-treasury-lp-preview)
  (let ((scaled-principal (var-get principal-scaled))
        (idx (var-get index))
        (next (next-index))
        (old-debt (mul-div-down scaled-principal idx INDEX-PRECISION))
        (new-debt (mul-div-down scaled-principal next INDEX-PRECISION))
        (debt-delta (if (> new-debt old-debt) (- new-debt old-debt) u0))
        (reserve-inc (mul-div-down debt-delta (var-get fee-reserve) BPS))
        (ta-preview (total-assets-preview)))
    (if (> reserve-inc u0)
        (mul-div-down reserve-inc (total-supply) (- ta-preview reserve-inc))
        u0)))
```
