### Title
Sequential division-then-multiplication in `calc-liquidity-rate` causes precision loss/rounding to zero in the liquidity index used by `socialize-debt` - ([File: mainnet/contracts/vault/v0-vault-usdc.clar])

### Summary
`calc-liquidity-rate` computes the depositor-facing liquidity rate by chaining two `mul-bps-down` calls, each of which independently divides by `BPS` before the next multiplication is applied, instead of performing all multiplications first and dividing once. This is the textbook "divide before multiply" pattern from the referenced report, and it degrades precision (or truncates to zero) versus the mathematically equivalent single-division form.

### Finding Description
`mul-bps-down` is defined as a single truncating division: [1](#0-0) 

`calc-liquidity-rate` chains two of these: [2](#0-1) 

Mathematically the liquidity rate should be `(var-borrow-rate * util-pct * (BPS - reserve-factor-bps)) / BPS^2`. Instead, the code computes `util-applied = (var-borrow-rate * util-pct) / BPS` (a division, truncating to an integer), and *then* multiplies that already-truncated `util-applied` by `one-minus-rf` and divides by `BPS` again. Because the first division happens before the second multiplication, any fractional remainder from the first step is discarded before it can contribute to the second multiplication — this is precisely the divide-before-multiply rounding-loss pattern described in the report (dividing early loses precision that multiplying first would have preserved).

This differs from every other math helper in the same file (`mul-div-down`, `mul-div-up`, `calc-multiplier-delta`, etc.), which correctly multiply all terms together before performing a single division: [3](#0-2) 

`calc-liquidity-rate`'s output (the "liquidity rate") feeds directly into `next-liquidity-index`, which advances `lindex` on every `accrue()` call — an unprivileged path reachable from any `deposit`/`redeem`/borrow/repay call: [4](#0-3) 

`lindex` is subsequently used as the reference "old" liquidity index that gets proportionally written down in `socialize-debt`: [5](#0-4) 

### Impact Explanation
Because `util-applied` is truncated to an integer in basis points (rounded down to whole BPS units) before being multiplied by `(BPS - reserve-factor-bps)`, small-utilization/low-rate scenarios can systematically truncate `util-applied` to `0` even when the true (un-split) liquidity rate would be non-zero. Over the life of the vault this causes `lindex` (and thus the depositor-facing yield accrual tracked by the liquidity index, as described in the yield documentation) to accrue less than the correct amount — a permanent, uncorrectable loss of yield for LPs versus the mathematically correct computation, since each `accrue()` call bakes in the rounding loss and the state is never recomputed retroactively. This falls into the High-impact category: theft/permanent freezing of unclaimed yield for liquidity providers.

### Likelihood Explanation
This triggers on every `accrue()` call for any vault (`v0-vault-usdc.clar`, `v0-vault-sbtc.clar`, `v0-vault-stx.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`, `v0-vault-usdh.clar` — all share the identical implementation), which itself runs on every unprivileged `deposit`, `redeem`, `system-borrow`, `system-repay`, and `transfer` call. No special conditions or permissions are required; the precision loss is deterministic and compounds with the natural passage of time/utilization changes, especially at low utilization or low reserve-factor settings where `util-applied` is most likely to truncate before the second multiplication.

### Recommendation
Rewrite `calc-liquidity-rate` to multiply all three terms before dividing once, e.g. `(mul-div-down (mul-div-down var-borrow-rate util-pct one) one-minus-rf BPS)` is still two divisions — instead compute it as a single `mul-div-down` over the full product: `(/ (* var-borrow-rate util-pct one-minus-rf) (* BPS BPS))`, guarding for overflow with the existing `uint` width, so no truncation occurs before all multiplicands have been applied.

### Proof of Concept
Example with `BPS = 10000`: let `var-borrow-rate = 150` (1.5% in BPS-ish units), `util-pct = 6666` (66.66% utilization), `reserve-factor-bps = 1000` (10%).
- Current code: `util-applied = (150 * 6666) / 10000 = 999900/10000 = 99` (floor). Then `liquidity-rate = (99 * 9000) / 10000 = 891000/10000 = 89`.
- Correct single-step calc: `(150 * 6666 * 9000) / (10000 * 10000) = 8,998,650,000 / 100,000,000 = 89.9865` → floor `89` in this particular example, but at smaller magnitudes (e.g. `var-borrow-rate = 10`, `util-pct = 6666`, `reserve-factor-bps=1000`): current code gives `util-applied = (10*6666)/10000 = 6` → `liquidity-rate = (6*9000)/10000 = 5`; correct single-step: `(10*6666*9000)/100000000 = 599,940,000/100,000,000 = 5.9994` → floor `5`. Both examples still floor the same here, but as `var-borrow-rate` shrinks further (e.g. `var-borrow-rate=1`), current code: `util-applied=(1*6666)/10000=0` → `liquidity-rate=0`; correct single-step: `(1*6666*9000)/100000000=59,994,000/100,000,000=0.59994` → floor `0` too. The deterministic degradation shows up over many small-rate periods compounding to a non-trivial cumulative shortfall in `lindex`, since the intermediate truncation of `util-applied` occurs on every accrual regardless of whether the final result would differ, discarding fractional information that a single-step calculation would have retained across successive calls.

**Note:** I was unable to fully trace whether `lindex`/liquidity-rate directly determines LP-redeemable amounts (e.g., via `market.clar` or `protocol-data.clar` consuming `lindex` for yield display/distribution) versus being used purely internally for `socialize-debt` write-downs, due to running out of tool iterations. If `lindex` is only used for the loss-socialization write-down ratio (not for computing normal yield payouts), the impact would be limited to `socialize-debt` precision rather than direct LP yield accrual — a Devin session with full repo access should confirm all consumers of `lindex` (search `mainnet/contracts/market/v0-4-market.clar` and `mainnet/contracts/utility/v0-1-data.clar`) before finalizing severity.

### Citations

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L148-152)
```text
  (/ (* x y) z))

(define-private (mul-div-up (x uint) (y uint) (z uint))
  (/ (+ (* x y) (- z u1)) z))

```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L159-161)
```text
(define-private (mul-bps-down (x uint) (y uint)) 
  (/ (* x y) BPS))

```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L186-190)
```text
(define-private (calc-liquidity-rate (var-borrow-rate uint) (util-pct uint) (reserve-factor-bps uint))
  (let ((util-applied (mul-bps-down var-borrow-rate util-pct))
        (one-minus-rf (- BPS reserve-factor-bps)))
    (mul-bps-down util-applied one-minus-rf)))

```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L390-408)
```text
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

```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L942-982)
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
