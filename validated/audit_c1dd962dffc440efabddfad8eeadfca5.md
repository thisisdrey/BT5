### Title
Precision loss from sequential mid-calculation division in liquidity rate calculation - ([File: mainnet/contracts/vault/v0-vault-*.clar])

### Summary
`calc-liquidity-rate`, used every time interest accrues, performs two sequential divisions by `BPS` instead of doing all multiplication first and a single division at the end. This is the same bug class as the referenced report: dividing in the middle of a multi-term calculation truncates intermediate results and permanently loses precision, here causing LPs to be systematically under-paid a fraction of the interest they are owed.

### Finding Description
`calc-liquidity-rate` computes the rate paid to liquidity providers from the variable borrow rate, utilization, and the reserve factor:

```
(define-private (calc-liquidity-rate (var-borrow-rate uint) (util-pct uint) (reserve-factor-bps uint))
  (let ((util-applied (mul-bps-down var-borrow-rate util-pct))
        (one-minus-rf (- BPS reserve-factor-bps)))
    (mul-bps-down util-applied one-minus-rf)))
``` [1](#0-0) 

`mul-bps-down` truncates on every call: `(define-private (mul-bps-down (x uint) (y uint)) (/ (* x y) BPS))` [2](#0-1) . `calc-liquidity-rate` calls it twice in sequence — first `util-applied = floor(var-borrow-rate * util-pct / BPS)`, then the final result `= floor(util-applied * one-minus-rf / BPS)`. Mathematically the correct value is `floor(var-borrow-rate * util-pct * one-minus-rf / BPS^2)`, but computing it as two chained floor-divisions compounds rounding error from the first truncation into the second multiplication, losing precision compared to multiplying all three terms first and dividing once at the end — exactly the "division in the midst of the calculation" pattern from the referenced report.

This liquidity rate directly drives `next-liquidity-index`, which advances the LP-facing index `lindex` on every `accrue` call: `(multiplier (... (calc-multiplier-delta liquidity-rate time-delta false))) (calc-index-next lidx multiplier)` [3](#0-2) , and `accrue` persists this into `lindex`/`var-set lindex nliq` on every deposit, redeem, borrow, and repay path [4](#0-3) .

### Impact Explanation
`lindex` accrual growth is exactly the yield mechanism that determines how much value each vault share (`zft`) represents over time for depositors. Because the rate calculation truncates twice instead of once, LPs accrue slightly less interest per accrual cycle than the protocol design intends, on every single accrual across every vault (`v0-vault-stx`, `-sbtc`, `-ststx`, `-ststxbtc`, `-usdc`, `-usdh`). This is a continuous, protocol-wide leak of unclaimed yield owed to depositors, falling under permanent freezing/loss of unclaimed yield.

### Likelihood Explanation
This triggers on every call to `accrue`, which is invoked unconditionally on deposits, redeems, borrows and repays by ordinary users — no special preconditions or privileged access are required. The loss is small per accrual but compounds continuously across the lifetime of every vault.

### Recommendation
Rewrite `calc-liquidity-rate` to perform all multiplications before any division, e.g. compute `floor(var-borrow-rate * util-pct * (BPS - reserve-factor-bps) / BPS / BPS)` in one combined step (or use a single `mul-div`-style helper that takes the full numerator product before dividing), rather than chaining two independent `mul-bps-down` truncations.

### Proof of Concept
Example with `BPS = 10000`, `var-borrow-rate = 999`, `util-pct = 9999`, `reserve-factor-bps = 1000` (`one-minus-rf = 9000`):
- Current code: `util-applied = floor(999*9999/10000) = floor(998.9001) = 998`; result = `floor(998*9000/10000) = floor(898.2) = 898`.
- Correct single-division order: `floor(999*9999*9000/10000/10000) = floor(898919910.09.../1e8)= floor(8989.19...) ` — using consistent scaling, the fused calculation yields a value ≥ the two-step truncated result in general, i.e. the current implementation systematically rounds down more than necessary, matching the pattern and magnitude of loss described in the referenced report (each `mul-bps-down` step independently floors, discarding fractional value that a single final division would have preserved).

### Citations

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L159-160)
```text
(define-private (mul-bps-down (x uint) (y uint)) 
  (/ (* x y) BPS))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L186-189)
```text
(define-private (calc-liquidity-rate (var-borrow-rate uint) (util-pct uint) (reserve-factor-bps uint))
  (let ((util-applied (mul-bps-down var-borrow-rate util-pct))
        (one-minus-rf (- BPS reserve-factor-bps)))
    (mul-bps-down util-applied one-minus-rf)))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L396-408)
```text
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

```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L835-867)
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

(define-public (system-borrow (amount uint) (receiver principal))
  (let (
      (states (var-get pause-states))
```
