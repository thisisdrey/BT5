### Title
Interest index (`next-index`/`next-liquidity-index`) applies current spot utilization rate retroactively over the full elapsed accrual window, enabling rate manipulation - (File: `mainnet/contracts/vault/v0-vault-stx.clar`)

### Summary
The vault's interest accrual logic computes the borrow/liquidity index update by multiplying the **current, spot** interest rate (derived from the **current** utilization) by the entire `time-delta` elapsed since the last accrual, rather than integrating the rate over that period as it actually evolved. This is the same root-cause pattern as the reported MagicLP TWAP issue: a value that is supposed to represent an average/accumulation over a past duration is instead computed from spot state and retroactively applied to the whole duration, giving an attacker who can move that spot state just before the read/write full control over the registered value for the entire elapsed period.

### Finding Description
`next-index` and `next-liquidity-index` compute the multiplier to apply to the stored index using `interest-rate()` (spot rate from `interpolate-rate` of the **current** `utilization`) and `time-delta = stacks-block-time - last-update`: [1](#0-0) 

`interest-rate` and `utilization` are both derived from the vault's current `available-assets`/`total-debt`, i.e. current reserves: [2](#0-1) 

`calc-multiplier-delta` then multiplies this spot `rate` by the *entire* `time-delta` (which may span many blocks/seconds since the vault was last touched) to build the compounding multiplier: [3](#0-2) 

`accrue` then commits this index (and mints the corresponding treasury LP share based on the resulting `debt-delta`) and stamps `last-update` at `stacks-block-time`, so once committed the retroactively-applied rate becomes permanent: [4](#0-3) 

This mirrors the reported bug exactly: in MagicLP, `_twapUpdate()` multiplies the **new** (post-manipulation) mid-price by `timeElapsed`, giving the attacker control of the cumulative price for the whole elapsed window. Here, the vault multiplies the **current** (attacker-influenced) rate by `time-delta` since the last accrual, giving the attacker control over the compounding applied for the whole elapsed window. Any user action that changes utilization immediately before an `accrue` is triggered (deposit, withdraw, borrow, or repay routed through the market) can be used to bias `interest-rate()` at the exact instant `time-delta` is "cashed in."

### Impact Explanation
Because the elevated/depressed spot rate is applied to the entire elapsed period rather than to the instant that follows, whichever direction an attacker pushes utilization right before triggering accrual gets amortized over the whole dormant window instead of over the brief moment the attacker actually held that utilization level:
- Pushing utilization up right before accrual inflates `debt-delta` for the whole elapsed period, unfairly increasing all borrowers' owed debt and inflating the treasury LP mint (`treasury-lp`) beyond what real accrued interest justifies, at existing borrowers'/suppliers' expense.
- Pushing utilization down right before accrual on a vault the attacker itself has outstanding debt in shrinks the interest attributed for the whole elapsed period, reducing the amount attributed to `debt-preview`/`total-debt` and thus underpaying suppliers who should have earned yield for that entire dormant window — this is a theft of unclaimed yield from the liquidity providers, since `lindex` growth (which determines redemption value / zToken price via `resolve-ztoken` in the market oracle) is permanently understated once `accrue` commits.

This lands on the in-scope **High** impact class: theft of unclaimed yield to LPs (or, in the debt-inflation direction, unfair/permanent overstatement of others' debt, bordering on protocol insolvency/fund freezing for affected borrowers).

### Likelihood Explanation
Exploitation requires only ordinary, unprivileged calls through the market's public deposit/borrow/repay/withdraw entry points (no DAO or privileged role needed) and is most profitable exactly when a vault has gone a long time without any accrual-triggering activity, so `time-delta` is large — a state that naturally occurs on lower-activity vaults (e.g. `stSTXbtc`, `USDH`). The attacker only needs enough capital to temporarily move utilization within a single transaction/block before another action (their own or anyone else's) triggers `accrue`.

### Recommendation
Do not apply the spot rate retroactively over the entire elapsed `time-delta`. Either (a) checkpoint and integrate the rate incrementally (accrue on every utilization-changing operation, not just periodically), or (b) require `accrue()` to be invoked as the very first, atomic step of any operation that can affect utilization, and disallow reading/using a stale `last-update` window combined with a rate computed after the utilization-changing action within the same transaction — i.e., snapshot/commit the rate-times-time contribution *before* utilization is changed, analogous to updating `_BASE_PRICE_CUMULATIVE_LAST_` before reserve changes in the referenced report.

### Proof of Concept
1. Vault `V` has been idle for a long `time-delta` (e.g. many hours) — `last-update` is stale, `index`/`lindex` unchanged.
2. Attacker calls market's supply/withdraw or borrow/repay path to sharply move `utilization()` (e.g., large flash-style deposit/borrow using owned capital) in a single transaction.
3. In the same transaction, attacker (or any subsequent caller) triggers `accrue` (e.g., via a tiny `system-repay`/`system-borrow` call reached through the market), which calls `next-index`/`next-liquidity-index`: [1](#0-0) 
4. `interest-rate()` is evaluated using the attacker's manipulated utilization and multiplied by the whole stale `time-delta` via `calc-multiplier-delta`: [3](#0-2) 
5. `accrue` commits the resulting `index`/`lindex` and mints `treasury-lp`, then stamps `last-update`, permanently baking in the manipulated retroactive rate: [4](#0-3) 
6. Attacker reverses their utilization-moving position (withdraws/repays) after accrual completes, having captured a mispriced index update applied across the entire dormant window instead of the instant they actually held that utilization.

### Citations

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L170-178)
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

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L379-402)
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
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L366-379)
```text
(define-private (utilization)
  (calc-utilization (get-available-assets) (total-debt)))

(define-private (interest-rate)
  (let ((points-data (var-get points-ir))
        (uword (get util points-data))
        (rword (get rate points-data))
        (utils (unpack-u16 uword))
        (rates (unpack-u16 rword)))
    (interpolate-rate (utilization) utils rates)))

(define-private (next-index)
  (let ((states (var-get pause-states))
        (idx (var-get index)))
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L841-866)
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
```
