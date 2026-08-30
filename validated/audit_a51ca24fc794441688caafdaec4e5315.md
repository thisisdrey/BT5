### Title
Same-block/timestamp borrow-repay yields fully interest-free debt due to per-timestamp interest accrual caching - ([File: mainnet/contracts/vault/v0-vault-stx.clar], [File: mainnet/contracts/market/v0-4-market.clar])

### Summary
Every Zest vault (`v0-vault-stx.clar`, `v0-vault-usdc.clar`, `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`, `v0-vault-usdh.clar`) only advances its borrow/liquidity index when `stacks-block-time` differs from the stored `last-update`. Any borrow and repay performed within the same Stacks-block timestamp accrues zero interest and mints zero protocol-reserve shares to `dao-treasury`, mirroring the Revert Lend M-04 finding exactly.

### Finding Description
`next-index` and `next-liquidity-index` compute the interest multiplier from `time-delta = stacks-block-time - last-update`; when `time-delta` is `u0` the multiplier is fixed to `INDEX-PRECISION` (no growth): [1](#0-0) 

`accrue` only mutates `index`/`lindex`/`last-update` and mints treasury-lp reserve shares when the new index differs from the old one — if `next-index` equals the current `index` (same-timestamp case), no state changes and no reserve is minted: [2](#0-1) 

Any ordinary principal can trigger this path through the unprivileged `market.clar` entry points. `borrow` calls `accrue-and-cache` (which caches the vault's index per `stacks-block-time`) then calls `vault-system-borrow`, which internally calls `accrue`: [3](#0-2) 

The corresponding `vault-stx.clar` `system-borrow`/`system-repay` functions each call `(accrue)` first and then mutate `principal-scaled`/`total-borrowed` using the (unchanged, in same-timestamp case) `idx`: [4](#0-3) [5](#0-4) 

Because `borrow`/`repay` scaled-debt conversion is entirely determined by `idx` (the borrow index), and `idx` is identical before and after when both calls occur under the same `stacks-block-time`, a caller who borrows and repays within the same timestamp pays back exactly the scaled-debt amount borrowed, i.e., zero interest, and the vault never mints the protocol's reserve-factor share to `dao-treasury` for that period.

This is the identical root cause pattern described in the referenced report's `_updateGlobalInterest()` (`if (block.timestamp > lastExchangeRateUpdate)`), applied here via `(if (get accrue states) ... (var-set last-update stacks-block-time))` gating in `accrue`.

### Impact Explanation
This falls under the in-scope "High - theft of unclaimed yield" category: liquidity providers and the DAO treasury lose the interest and reserve-factor income they would otherwise earn on the borrowed capital for that period. A user (or repeated automated caller) can structure borrow+repay pairs so that the intervening `stacks-block-time` never changes, permanently denying yield accrual on that principal for the duration it is held. Since Stacks blocks can share timestamps and transactions can be sequenced within a block, this is repeatable at scale, similar to the referenced report's whale/MEV scenario, and directly reduces LP/treasury unclaimed yield.

### Likelihood Explanation
Likelihood is limited by the cost of repeatedly executing borrow/repay transactions and by the requirement of holding sufficient collateral to satisfy health checks in `market.clar`'s `borrow`/`repay` flow, but no additional fee or friction (e.g., borrow origination fee) exists in the vault `system-borrow` path to disincentivize this, unlike the flashloan path which does charge `fee-flash`. Ordinary borrow/repay is not subject to any such fee, so the attack is economically feasible for a well-collateralized principal, matching the Medium/borderline-High assessment in the referenced report.

### Recommendation
Add a small borrow-origination fee (a percentage of borrowed amount, charged in `system-borrow` in each `v0-vault-*.clar`, independent of index-based interest) so that same-timestamp/same-block borrow-repay cycles are not entirely interest-free, similar to how `fee-flash` already penalizes flashloan usage. Alternatively, ensure a minimum interest is always accrued on `system-borrow`/`system-repay` regardless of whether `stacks-block-time` has advanced.

### Proof of Concept
1. Vault `index`/`lindex` are cached with `last-update` set to the block timestamp at time T.
2. In a transaction sequence executed while `stacks-block-time` remains T (same Stacks block), an unprivileged caller invokes `market.clar` `borrow` for asset X → this calls `vault-system-borrow` → `accrue` runs `next-index`, computing `time-delta = T - T = 0` → multiplier stays at `INDEX-PRECISION` → `index` unchanged, no treasury-lp minted [6](#0-5) .
3. Still within timestamp T, the same caller invokes `market.clar` `repay` for the full borrowed amount → `vault-system-repay` runs `accrue` again with the same zero `time-delta`, then computes `principal-repaid`/`interest-paid` using the unchanged `idx` [5](#0-4) .
4. Because `idx` never advanced, `interest-paid` is `u0` and the exact principal borrowed is repaid, with zero yield to LPs and zero reserve minted to `dao-treasury`.

### Citations

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L379-404)
```text
(define-private (next-index)
  (let ((states (var-get pause-states))
        (idx (var-get index)))
    (if (get accrue states)
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
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L843-900)
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
    (asserts! (<= amount available-assets) ERR-INSUFFICIENT-VAULT-LIQUIDITY)
    (asserts! (<= (+ debt amount) CAP-DEBT) ERR-DEBT-CAP-EXCEEDED)

    (var-set principal-scaled updated-scaled-principal)
    (var-set total-borrowed (+ (var-get total-borrowed) amount))
    (try! (send-underlying amount receiver))

    (print {
      action: "system-borrow",
      caller: contract-caller,
      data: {
        receiver: receiver,
        amount: amount,
        scaled-amount: scaled-amount,
        principal-scaled: updated-scaled-principal,
        total-borrowed: (var-get total-borrowed),
        index: idx
      }
    })

    (ok true)))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L902-920)
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

    (try! (check-caller-auth))
    (asserts! (not (get repay states)) ERR-PAUSED)
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1276-1319)
```text
    (let ((future-mask (bit-or mask (pow u2 (+ asset-id DEBT-OFFSET))))
          (future-group (try! (get-egroup future-mask)))
          ;; Per-egroup borrow disable check (uses FUTURE egroup, not current)
          ;; Each bit in BORROW-DISABLED-MASK corresponds to a debt asset ID (NOT offset by 64)
          (disabled-borrow-mask (get BORROW-DISABLED-MASK future-group))
          (debt-increase (try! (get-asset-value asset amount true)))
          (debt-post-increased (+ debt-value debt-increase)))

    ;; Check if this specific asset is disabled for borrowing in the FUTURE egroup
    (asserts! (is-eq (bit-and disabled-borrow-mask (pow u2 asset-id)) u0) ERR-EGROUP-ASSET-BORROW-DISABLED)
    ;; postconditions
    (asserts! (try! (is-healthy-with-mask collateral-value debt-post-increased future-mask)) ERR-UNHEALTHY)

    (try! (vault-system-borrow asset-id amount funds-receiver))
    (let ((scaled-debt-added (convert-to-scaled-debt asset-id amount true))
          (borrow-index (get index (unwrap-panic (get-cached-indexes asset-id)))))
      (try! (contract-call? .v0-market-vault
                            debt-add-scaled
                            account
                            scaled-debt-added
                            asset-id))
      
      (print {
        action: "borrow",
        caller: contract-caller,
        data: {
          account: account,
          receiver: funds-receiver,
          asset-id: asset-id,
          asset-addr: address,
          amount: amount,
          scaled-debt-added: scaled-debt-added,
          borrow-index: borrow-index,
          position-collateral-usd: collateral-value,
          position-debt-usd: debt-post-increased
        }
      })
      
      (ok true)))))

(define-public (repay (ft <ft-trait>) (amount uint) (on-behalf-of (optional principal)))
  (let ((address (contract-of ft))
        (asset (try! (get-asset address)))
        (asset-id (get id asset))
```
