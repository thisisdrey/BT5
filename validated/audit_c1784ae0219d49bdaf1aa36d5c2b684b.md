### Title
Protocol reserve fee (`reserve-inc`) permanently truncates to zero on frequent small `accrue()` calls, denying DAO treasury of yield - (File: `mainnet/contracts/vault/v0-vault-usdc.clar` and equivalent vault contracts)

### Summary
This is a valid analog of the reported bug class ("fees wrongly calculated due to integer-division truncation"). In the Zest vaults, the protocol's reserve-factor cut of accrued interest (`reserve-inc`) is computed with a floor-division (`mul-div-down`), so it truncates to `0` whenever the interest accrued between two calls to `accrue` is small relative to `BPS`. Because `accrue` is a public, unauthenticated function that advances the borrow/liquidity index (permanently consuming the interest delta), any ordinary principal can repeatedly call it with short intervals to keep every `debt-delta` below the rounding threshold, causing the DAO treasury to permanently lose its share of interest revenue across the whole life of the vault.

### Finding Description
`accrue()` in each vault (e.g. `v0-vault-usdc.clar`) computes: [1](#0-0) 

```
(old-debt (mul-div-down scaled-principal idx INDEX-PRECISION))
(new-debt (mul-div-down scaled-principal next INDEX-PRECISION))
(debt-delta (if (> new-debt old-debt) (- new-debt old-debt) u0))
(reserve-inc (mul-div-down debt-delta (var-get fee-reserve) BPS))
(treasury-lp (if (> reserve-inc u0) (mul-div-down reserve-inc (total-supply) (- (total-assets-preview) reserve-inc)) u0))
``` [1](#0-0) 

`reserve-inc` is `debt-delta * fee-reserve / BPS` using floor division (`BPS = u10000`) [2](#0-1) . Whenever `debt-delta * fee-reserve < 10000`, `reserve-inc` rounds down to `0`, so no `treasury-lp` shares are minted for that call — exactly the truncation bug described in the report (`grant.value/100` rounding to `0` when `grant.value < 100`).

Critically, `accrue()` also unconditionally advances the cached `index`/`lindex` on every call (once `debt-delta > 0`) [3](#0-2) , meaning the interest bracket associated with that truncated `debt-delta` is consumed and can never be re-taxed later — the loss is permanent, not merely deferred. Unlike `system-borrow`, which calls `check-caller-auth` [4](#0-3) , `accrue` has no caller-authorization check — it is gated only by the `accrue` pause flag [5](#0-4) , so it is callable by any unprivileged principal (directly or via `deposit`/`redeem`/`system-borrow`/`system-repay`, which all invoke it internally).

An attacker can therefore repeatedly call `accrue` at short intervals (every block, or the minimum interval that still produces `time-delta > 0` in `next-index`/`next-liquidity-index` [6](#0-5) ) so that each `debt-delta` stays under the rounding threshold, guaranteeing `reserve-inc == 0` on every call and preventing the DAO treasury from ever receiving its reserve-factor share of interest — even though the same total interest, accrued in fewer/larger steps, would have produced non-zero `treasury-lp` mints.

### Impact Explanation
This causes permanent loss of protocol fee revenue that should have been minted as `zft` shares to `.dao-treasury` [7](#0-6) . Since the index advances on each call, the un-taxed interest delta is not recoverable later — it is permanently forfeited to depositors instead of the treasury. This matches the in-scope impact class "permanent freezing of unclaimed yield" for the DAO treasury: the protocol's designed revenue stream (reserve factor on interest) can be driven to zero indefinitely across all vaults (`v0-vault-usdc`, `v0-vault-usdh`, `v0-vault-stx`, `v0-vault-sbtc`, `v0-vault-ststx`, `v0-vault-ststxbtc`) by any unprivileged caller.

### Likelihood Explanation
`accrue()` is public, unauthenticated, and cheap to call. An attacker only needs to call it once per qualifying interval (any positive `time-delta`) to keep `debt-delta` — and hence `reserve-inc` — under the truncation threshold. Given typical `fee-reserve` values (a fraction of `BPS = 10000`) and realistic per-call interest deltas on partially-utilized markets, this threshold is easily kept below by frequent calling, making this a low-cost, repeatable, permanent denial of protocol fee revenue.

### Recommendation
- Accumulate rounding remainders across `accrue()` calls (carry a `fee-dust` accumulator) instead of discarding the truncated remainder each time, and mint treasury shares once the accumulator crosses a whole-unit threshold.
- Alternatively, use `mul-div-up` (or a comparable ceiling/round-nearest routine) for `reserve-inc`, or compute the treasury share as `debt-delta - (debt-delta * (BPS - fee-reserve) / BPS)` so both shares reconcile exactly to `debt-delta` without silently dropping the protocol's portion.
- Consider rate-limiting or batching `accrue()` triggers (e.g., minimum interval) so interest deltas per call stay large enough that rounding losses become economically negligible relative to accrued interest.

### Proof of Concept
1. Deploy/observe a vault (e.g. `v0-vault-usdc.clar`) with `fee-reserve` set to a typical value (e.g., `1000` = 10% in BPS terms, `BPS = 10000`).
2. As any unprivileged principal, repeatedly call `accrue()` (or any of `deposit`/`redeem`/`system-borrow`/`system-repay` which invoke it) at the minimum interval that produces `time-delta > 0` in `next-index` [8](#0-7) .
3. Each call computes a small `debt-delta` such that `debt-delta * fee-reserve / BPS == 0`, so `reserve-inc == 0` and no `treasury-lp` is minted to `.dao-treasury` [9](#0-8) .
4. Despite this, `index`/`lindex` still advance and `last-update` is refreshed [3](#0-2) , so the interest bracket is consumed — the treasury permanently loses that portion of the protocol fee, indefinitely, as long as the attacker keeps triggering frequent small accruals instead of allowing larger accrual intervals.

### Citations

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L25-25)
```text
(define-constant BPS u10000)
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L380-402)
```text
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

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L837-864)
```text
      (if (get accrue states)
          ;; PAUSED: Pass-through without reverting
          (ok { index: idx, lindex: lidx })
          ;; NOT PAUSED: Normal accrual logic
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
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L877-877)
```text
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)
```
