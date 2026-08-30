Based on my investigation, I found a strong analog to the "rewards lost due to rounding down error" bug class in Zest's treasury-LP minting logic within the vault contracts' `accrue` function.

### Title
Rounding down of `reserve-inc` causes protocol reserve (DAO treasury) yield to be silently lost on frequent small accruals - ([File: mainnet/contracts/vault/v0-vault-stx.clar])

### Summary
The vault's `accrue` function computes the protocol's reserve share of newly accrued interest (`reserve-inc`) using integer division (`mul-div-down`), and only mints treasury shares (`treasury-lp`) when `reserve-inc > 0`. When `debt-delta` (interest accrued since the last update) multiplied by `fee-reserve` and divided by `BPS` rounds down to zero, the treasury receives no shares for that accrual — even though the interest itself was still fully credited to `total-debt`/suppliers. This mirrors the reported bug class where `_amount * POINTS_MULTIPLIER / shares` rounding to 0 silently drops rewards owed to a party.

### Finding Description
In `accrue`, the reserve/treasury minting path is: [1](#0-0) 

- `debt-delta` is the newly accrued interest for this call: `new-debt - old-debt` where both use `mul-div-down`.
- `reserve-inc` is computed as `mul-div-down(debt-delta, fee-reserve, BPS)`.
- `treasury-lp` is minted to `.dao-treasury` only `if (> reserve-inc u0)`, using `mul-div-down` again against `(total-assets-preview) - reserve-inc)`.

The same pattern occurs identically in `calc-treasury-lp-preview`, used for share-price previews: [2](#0-1) 

Because `accrue` is invoked on essentially every state-changing vault operation (`system-borrow`, `deposit`, `redeem`, etc., each first calling `(try! (accrue))`), and the market's per-block index cache further limits how often `accrue` actually recomputes fresh indexes within a block, an ordinary sequence of small, frequent transactions (e.g., low-value deposits/borrows spaced by short time intervals, or many separate blocks with small elapsed time and low `interest-rate`/`utilization`) will repeatedly produce a small nonzero `debt-delta` whose `debt-delta * fee-reserve / BPS` rounds down to `0`. In each such call, `pointPerShare`-equivalent state (the `index`/`lindex` values) is still advanced to `next`/`nliq` (crediting the full accrued interest to depositors), but the corresponding fraction reserved for the protocol treasury (`reserve-inc`, meant to be skimmed as `fee-reserve` bps of that interest) is dropped to zero and no `treasury-lp` is minted. This is structurally identical to the reported issue: the reward variable that should always move forward proportionally to the transferred/accrued amount can silently round to zero and be permanently lost, because there is no `require`/assert that `reserve-inc` (or the treasury mint) tracks a nonzero minimum contribution, nor any accumulation of the "remainder" for later capture.

### Impact Explanation
This causes permanent loss of protocol reserve yield (an unclaimed-yield stream owed to `.dao-treasury`) whenever accrued interest per call is small relative to `BPS`/`fee-reserve` — a condition that recurs continuously in normal vault operation (frequent low-value or closely-spaced transactions, low utilization periods, low interest rates). Each individual loss is small, but it is systematic and repeats every time `debt-delta * fee-reserve / BPS` rounds to 0, resulting in cumulative, unrecoverable underpayment of the protocol's reserve factor across the life of the vault. This falls under "permanent freezing/loss of unclaimed yield," matching the in-scope High impact category (theft/permanent loss of unclaimed protocol yield), since the underlying interest has already been credited to `total-debt`/suppliers, but the treasury's cut of it is dropped with no mechanism to recover it later.

### Likelihood Explanation
Likelihood is high in practice: this does not require any adversarial action — it happens automatically under ordinary usage whenever accrual intervals are short or interest amounts are small, which is common for vaults with low `fee-reserve` (few bps) or under low interest-rate/utilization regimes, and is exacerbated by the per-block index-cache (`accrue-and-cache` in `mainnet/contracts/market/v0-4-market.clar`) that already limits accrual granularity, meaning any residual rounding loss on top of that caching is compounded across many transactions rather than corrected. [3](#0-2) 

### Recommendation
Track and carry forward the rounding remainder (e.g., accumulate `debt-delta * fee-reserve % BPS` in a persistent variable and add it into the next `reserve-inc` calculation), or increase the internal precision used for `reserve-inc`/`treasury-lp` computation so that fractional reserve amounts are not truncated to zero on each individual accrual, ensuring the protocol reserve share converges to the intended `fee-reserve` proportion of total accrued interest over time rather than being lossy per-call.

### Proof of Concept
1. Configure a vault with a small `fee-reserve` (e.g., 100 bps) and low `interest-rate`/`utilization` so that per-call `debt-delta` is small (e.g., a handful of base units of the underlying asset).
2. Trigger `accrue` repeatedly via routine operations (`deposit`, `system-borrow`, `redeem`), each in a different block/timestamp so the market's index cache does not suppress accrual: `debt-delta = new-debt - old-debt` is nonzero but tiny. [4](#0-3) 
3. For each such call, compute `reserve-inc = debt-delta * fee-reserve / BPS`; when `debt-delta * fee-reserve < BPS`, `reserve-inc` rounds to `0`, so the `(if (> treasury-lp u0) (try! (ft-mint? zft treasury-lp .dao-treasury)) false)` branch never mints anything for that accrual, even though `index`/`lindex` (and thus supplier-facing debt/interest) were fully updated. [5](#0-4) 
4. Repeating this over many blocks/transactions demonstrates the treasury's reserve share is persistently and cumulatively lost relative to the intended `fee-reserve` percentage of total interest accrued, with no compensating mechanism anywhere in the vault contract.

### Citations

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L350-359)
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
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L843-863)
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

**File:** mainnet/contracts/market/v0-4-market.clar (L245-257)
```text
(define-private (accrue-and-cache (aid uint))
  (let ((cache-key { timestamp: stacks-block-time, aid: aid })
        (cached? (map-get? index-cache cache-key)))

    (match cached?
      ;; cache HIT: return cached value (1 read only)
      cached-indexes (ok cached-indexes)

      ;; cache MISS: accrue and cache (vault-accrue now returns indexes)
      (let ((indexes (try! (vault-accrue aid))))
        ;; store in cache
        (map-set index-cache cache-key indexes)
        (ok indexes)))))
```
