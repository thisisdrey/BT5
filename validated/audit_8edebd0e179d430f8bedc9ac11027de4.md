### Title
Truncated division in `next-liquidity-index` combined with shared `last-update` gating causes permanent loss of accrued interest owed to depositors - (File: `mainnet/contracts/vault/v0-vault-usdc.clar` and identical logic in `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`, `v0-vault-usdh.clar`)

### Summary
Every Zest vault computes two separate compounding indices — a borrow `index` (rounded **up**) and a `lindex` liquidity/supply index (rounded **down**) — from the same elapsed `time-delta`, but `accrue()` only rewinds `last-update` to the current block time when **either** index changes. Because the borrow index rounds up and the liquidity index rounds down, small/frequent `time-delta`s can produce a non-zero change in `index` while `next-liquidity-index` truncates to zero. When that happens, `last-update` is still advanced, and the elapsed time that should have compounded into the liquidity index is discarded forever — the exact truncated-division reward-loss pattern described in the external Locke.sol report.

### Finding Description
`calc-multiplier-delta` performs `mul-div-up`/`mul-div-down` on `rate * time-delta * INDEX-PRECISION / SECONDS-PER-YEAR-BPS`, with the rounding direction chosen by the caller: [1](#0-0) 

`next-index` (borrow index) always rounds **up** (`true`), while `next-liquidity-index` always rounds **down** (`false`): [2](#0-1) 

`accrue()` computes both `next` (borrow index) and `nliq` (liquidity index) from the identical `time-delta`, but only advances `last-update` to `stacks-block-time` if **either** index actually changed: [3](#0-2) 

Because the round-up borrow multiplier needs a smaller numerator to register a +1 change than the round-down liquidity multiplier needs to register any change at all, there exists a window of `time-delta` values where `index` changes (even by the smallest unit) but `lindex` truncates back to exactly the same value (no accrual for depositors). In that case line 858 (`(if (or (not (is-eq idx next)) (not (is-eq lidx nliq))) (var-set last-update stacks-block-time) false)`) still resets `last-update`, discarding the elapsed time for the liquidity-side calculation. The next `accrue()` call starts its `time-delta` from the new `last-update`, so the lost window of liquidity accrual can never be recovered — the interest that should have compounded into `lindex` for depositors is permanently gone, even though borrowers are still charged (their `index` grew).

`accrue()` is triggered from ordinary, permissionless, frequent user flows (`deposit`, `redeem`, `system-borrow`, `repay`), so any depositor or borrower interacting with the vault repeatedly and quickly (e.g., in consecutive blocks) can trigger this truncation pattern without any special privilege, exactly mirroring the report's condition "Reward is updated too frequently."

### Impact Explanation
This is a **High** severity issue: theft/loss of unclaimed yield. Depositors' entitlement to interest is tracked via the liquidity index (`lindex`), and each truncated round permanently and silently forfeits the interest they should have accrued for that elapsed period, while borrowers continue to be charged based on the borrow `index`, which rounds in the protocol's favor and is far less likely to truncate to zero. Repeated small-interval interactions (which cost nothing extra to an attacker or normal high-frequency users/bots) compound this loss over time, reducing the effective yield paid out to all depositors of a given vault.

### Likelihood Explanation
Likelihood is elevated by the fact that `accrue()` is reachable from unprivileged, ordinary operations (`deposit`, `redeem`, `system-borrow`) that any user can call at will, and low-decimal reward/utilization scenarios or low `time-delta` (e.g., consecutive block calls) make the round-down liquidity multiplier truncate to `INDEX-PRECISION` (no growth) far more easily than the round-up borrow multiplier truncates to no growth. No governance or oracle manipulation is required — only repeated normal calls.

### Recommendation
Track `last-update` independently for the borrow index and the liquidity index (or only advance `last-update` when a minimum multiplier increment is guaranteed for both), so that truncated-to-zero periods are retained and can compound into a future call instead of being discarded. Alternatively, accumulate a "pending time" component so unaccounted micro-intervals are not lost when only one of the two indices advances.

### Proof of Concept
1. Attacker/user calls `deposit` (or any function that calls `accrue`) at block `T0`, initializing `last-update = T0`, `index = INDEX-PRECISION`, `lindex = INDEX-PRECISION`.
2. At block `T0 + Δt` (small, e.g., a few seconds/one block), call `redeem` or `deposit` again to trigger `accrue()`:
   - `next-index` computes `mul-div-up(rate, Δt*INDEX-PRECISION, SECONDS-PER-YEAR-BPS)` — rounds up, may add `+1` to `index`.
   - `next-liquidity-index` computes `mul-div-down(liquidity-rate, Δt*INDEX-PRECISION, SECONDS-PER-YEAR-BPS)` — rounds down, truncates to `0`, so `lindex` stays unchanged.
3. Since `index != next`, line 858's `or` condition is true, so `last-update` is reset to `T0 + Δt`.
4. The liquidity index never accounts for the interval `[T0, T0+Δt]`; that portion of interest owed to depositors is permanently lost, and every subsequent `accrue()` call only computes `time-delta` from `T0+Δt` onward.
5. Repeating this pattern (e.g., automated frequent calls) systematically erodes depositor yield across the vault's lifetime while borrower debt continues to accrue normally via `index`. [4](#0-3)

### Citations

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L147-152)
```text
(define-private (mul-div-down (x uint) (y uint) (z uint))
  (/ (* x y) z))

(define-private (mul-div-up (x uint) (y uint) (z uint))
  (/ (+ (* x y) (- z u1)) z))

```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L170-179)
```text
(define-private (calc-multiplier-delta (rate uint) (time-delta uint) (round-up bool))
  (+ INDEX-PRECISION
    (if round-up
      (mul-div-up rate
                  (* time-delta INDEX-PRECISION)
                  SECONDS-PER-YEAR-BPS)
      (mul-div-down rate
                  (* time-delta INDEX-PRECISION)
                  SECONDS-PER-YEAR-BPS))))

```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L377-403)
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

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L833-861)
```text
(define-public (accrue)
  (let ((states (var-get pause-states))
        (idx (var-get index))
        (lidx (var-get lindex)))
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
```
