### Title
Interest-rate curve `util` and `rate` point arrays can be updated independently, desynchronizing the piecewise-linear interpolation used for accrual - ([File: mainnet/contracts/vault/v0-vault-stx.clar])

### Summary
Each vault contract stores its interest-rate curve as a single data var `points-ir` holding two independently packed point arrays, `util` and `rate` [1](#0-0) . Two separate DAO-gated setters, `set-points-util` and `set-points-rate`, each mutate only one side of this pair [2](#0-1) . `interest-rate` unpacks both arrays and feeds them together into `interpolate-rate`, which assumes the two arrays are the same length and index-aligned [3](#0-2) . This mirrors the Merit Circle H-3 bug class: a shared piecewise-curve structure (`curve`/`unit` there, `util`/`rate` points here) can be mutated through independent entry points that update only part of the coupled state, breaking the index correspondence the interpolation function relies on.

### Finding Description
`points-ir` is defined as `{util: uint, rate: uint}`, where each field is a packed encoding (via `pack-u16`) of up to 8 curve points [1](#0-0) . `set-points-util` re-packs only the `util` side and writes it back into `points-ir`, leaving `rate` untouched [4](#0-3) . Symmetrically, `set-points-rate` re-packs only `rate`, leaving `util` untouched [5](#0-4) . Both are independent, single-field updates with no validation that the newly supplied point list's length matches the other field's existing point list length.

`interest-rate` reads `points-ir`, unpacks both fields into `utils` and `rates` lists, and passes them together to `interpolate-rate(utilization, utils, rates)`, which walks the two lists in lockstep to find the interpolation segment and slope [3](#0-2) . This is structurally identical to `getMultiplier()` in the reported bug, which combines `curve[n]` with a `unit` derived from `curve.length` - if the two pieces of state that must stay length-matched are updated by different, uncoordinated setters, the resulting index lookups misalign, producing either invalid indices or a mismatched slope calculation between the utilization breakpoints and their corresponding rates.

Because `accrue` is invoked before the point update but the point update itself is applied afterward with no re-validation against the sibling array, any DAO/gov update to only one of the two arrays (e.g., adding a point to `util` without doing the corresponding update to `rate`, or vice versa) leaves the vault's `interest-rate` function operating on a curve whose `util` breakpoints no longer correspond 1:1 to `rate` values, exactly the "unit not recalculated" class of bug from the source report.

### Impact Explanation
`interest-rate` output feeds directly into `next-index` and `next-liquidity-index`, which drive per-block accrual of borrower debt and depositor liquidity index [6](#0-5) . A desynchronized util/rate curve computes an incorrect interest rate for a given utilization, so debt accrues faster or slower than the intended curve dictates and the liquidity index (depositor yield) grows incorrectly in the same direction. This is a mispricing of unclaimed yield/interest for lenders and borrowers - i.e., theft or freezing of unclaimed yield, matching the in-scope High impact class.

### Likelihood Explanation
This requires the DAO to call `set-points-util` and `set-points-rate` as two separate transactions (as designed) without keeping the point-list lengths synchronized. It is not a compromise scenario -- it is a routine governance action (adjusting the interest-rate curve, e.g., adding/removing a breakpoint), identical in nature to the original report's `setCurvePoint()` being a normal gov operation, and the contract provides no guard preventing a length mismatch between the two independently-set arrays.

### Recommendation
Merge `set-points-util` and `set-points-rate` into a single admin entry point (or require both lists to be submitted together and validated) so that `util` and `rate` are always updated atomically and their lengths are asserted equal before being written to `points-ir`, mirroring the recommended fix of recalculating/validating the coupled state on every mutation rather than as two independent operations.

### Proof of Concept
1. DAO calls `set-points-util` with an 8-point list, `check-dao-auth` passes, `points-ir.util` is updated to encode 8 breakpoints [4](#0-3) .
2. DAO separately calls `set-points-rate` with only a 3-point list (e.g., simplifying the curve), updating `points-ir.rate` to encode 3 rate values while `util` still encodes 8 breakpoints [5](#0-4) .
3. Any subsequent call to `accrue`/`interest-rate` unpacks `utils` (length 8) and `rates` (length 3) and passes both to `interpolate-rate`, which indexes them assuming matched lengths [3](#0-2) , producing an incorrect rate (or breaking assumptions of the interpolation) for the current utilization, corrupting `index`/`lindex` accrual going forward for every depositor and borrower.

### Citations

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L93-96)
```text
;; -- Interest rate
(define-data-var points-ir
  {util: uint, rate: uint}
  {util: u0, rate: u0})
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L666-702)
```text
(define-public (set-points-util (points (list 8 uint)))
    (let (
          (packed (unwrap-panic (pack-u16 points (some BPS))))
          (pir (var-get points-ir)))
      (try! (check-dao-auth))
      (try! (accrue))
      (var-set points-ir { util: packed, rate: (get rate pir) })
      
      (print {
        action: "vault-set-points-util",
        caller: tx-sender,
        data: {
          vault: UNDERLYING,
          points: points
        }
      })
      
      (ok true)))

(define-public (set-points-rate (points (list 8 uint)))
    (let (
          (packed (unwrap-panic (pack-u16 points none)))
          (pir (var-get points-ir)))
      (try! (check-dao-auth))
      (try! (accrue))
      (var-set points-ir { util: (get util pir), rate: packed })
      
      (print {
        action: "vault-set-points-rate",
        caller: tx-sender,
        data: {
          vault: UNDERLYING,
          points: points
        }
      })
      
      (ok true)))
```

**File:** local-testing/contracts/vault/vault-ststxbtc.clar (L375-381)
```text
(define-private (interest-rate)
  (let ((points-data (var-get points-ir))
        (uword (get util points-data))
        (rword (get rate points-data))
        (utils (unpack-u16 uword))
        (rates (unpack-u16 rword)))
    (interpolate-rate (utilization) utils rates)))
```

**File:** local-testing/contracts/vault/vault-ststxbtc.clar (L383-408)
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
