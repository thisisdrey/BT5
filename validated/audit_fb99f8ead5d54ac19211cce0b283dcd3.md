### Title
`accrue` advances `last-update` on partial index update, permanently discarding liquidity-index growth when `lindex`'s round-down truncates to zero while `index` still advances - (File: mainnet/contracts/vault/v0-vault-sbtc.clar)

### Summary
`accrue` computes `next-index` (round-up, debt side) and `next-liquidity-index` (round-down, supply side) independently from the same `time-delta`, but then unconditionally resets `last-update` to `stacks-block-time` if *either* value changed [1](#0-0) . When utilization/liquidity-rate is low enough that `calc-multiplier-delta` rounds `lindex`'s delta down to zero for a given small `time-delta` while `index`'s round-up branch still advances, that window's liquidity accrual is discarded forever since `last-update` no longer includes it in the next computation.

### Finding Description
`next-index` and `next-liquidity-index` both use `time-delta = stacks-block-time - last-update` and call `calc-multiplier-delta` with `round-up=true` for the debt index and `round-up=false` for the liquidity index [2](#0-1) . For small `time-delta` and low `liquidity-rate` (i.e., low utilization), the round-down `mul-div-down` inside `calc-multiplier-delta` for the liquidity leg can truncate to a multiplier of `INDEX-PRECISION` (i.e., zero growth), while the round-up leg for the debt index still increments by at least 1 unit.

In `accrue`, the two updates are applied independently, but the shared `last-update` timestamp is advanced if *either* changed:
```
(if (not (is-eq idx next))
    (var-set index next)
    false)
(if (not (is-eq lidx nliq))
    (var-set lindex nliq)
    false)
...
(if (or (not (is-eq idx next)) (not (is-eq lidx nliq)))
    (var-set last-update stacks-block-time)
    false)
``` [3](#0-2) 

Since `time-delta` for the *next* call to `next-liquidity-index` is computed from the new `last-update`, any window where `lindex` didn't move (due to round-down truncation) but `index` did move is never re-included in a future liquidity computation - the elapsed time for that window is permanently lost from the liquidity side's accrual base. An attacker can force this by calling `deposit` (which triggers `accrue`) every block with a minimal `amount=1`, at times when the debt index's round-up mechanically advances but the liquidity index's round-down does not, at low utilization.

Any unprivileged account can call `deposit` with `amount=1`, `min-out=u0`, `recipient=attacker` [4](#0-3)  - no permission, cap, or slippage check in the deposit path prevents this per-block repetition, and no existing check ties `last-update` advancement to both indices moving together.

### Impact Explanation
This matches the "High - theft of unclaimed yield or royalties" category: repeated triggering causes real (positive) interest accrued on the debt side to never be reflected proportionally in `lindex`, meaning the yield that should flow to zsBTC/LP holders (and treasury fee-reserve minting, which is itself keyed off `debt-delta` between `idx`/`next`) is under-accrued on the supply side over time, permanently losing that portion of yield for depositors while borrowers still effectively see their debt growing per the debt index. This does not toun steal funds at rest, so Critical (direct principal theft/insolvency) does not directly apply.

### Likelihood Explanation
The attack requires no special capital - only enough to pay gas and `amount=1` sBTC per call, and is repeatable every block. The precondition (utilization/liquidity-rate low enough that `mul-div-down` rounds the liquidity multiplier to zero for a given small `time-delta`, while the round-up debt multiplier still advances) is plausible whenever utilization is low and blocks are spaced closely, which is a normal, attacker-reachable market condition, not an edge case requiring adversarial oracle or governance influence.

### Recommendation
Only advance `last-update` when a shared, minimum unit of `time-delta` has been fully captured by both indices, or track `last-update` per-index / accumulate un-consumed remainder time-delta rather than resetting the base window whenever only one side changes. E.g., only update `last-update` when both `index` and `lindex` change, or keep a separate "un-applied elapsed time" accumulator until both computations produce nonzero multipliers.

### Proof of Concept
Clarinet/vitest simnet plan:
1. Initialize vault with low utilization (small `total-borrowed` relative to `assets`) so `interpolate-rate`/`calc-liquidity-rate` yields a small `liquidity-rate`.
2. Loop N times: mine 1 block, then call `deposit(amount=1, min-out=u0, recipient=attacker)` (which internally calls `accrue`).
3. After N iterations, read `lindex` and `index` from contract state, and independently compute the theoretical liquidity accrual as `total-borrowed * liquidity-rate * N-blocks / SECONDS-PER-YEAR-BPS` using a helper/read-only call that bypasses the truncation bug (e.g., replaying with a single large `time-delta` covering all N blocks at once).
4. Assert that the sum of the actual per-block `lindex` deltas is strictly less than the theoretical single-window liquidity accrual, while the equivalent single-window vs. per-block sum for `index` (debt side) match (or are much closer), demonstrating loss is specific to the liquidity/supply leg due to the `last-update` truncation bug identified in `accrue`.

### Citations

**File:** mainnet/contracts/vault/v0-vault-sbtc.clar (L377-402)
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

**File:** mainnet/contracts/vault/v0-vault-sbtc.clar (L833-861)
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
