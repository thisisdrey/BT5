### Title
Treasury reserve-share minting rounds down to zero as vault `total-assets` grows large, permanently losing accrued protocol yield - (`mainnet/contracts/vault/v0-vault-stx.clar` and equivalent vault contracts)

### Summary
The reported bug class is integer-division truncation that becomes materially worse as the base value in the ratio grows large (`totalVotingPower` in the original report). The strongest reachable analog in Zest is the treasury-share minting math inside every vault's `accrue` function: `treasury-lp = reserve-inc * total-supply / (total-assets-preview - reserve-inc)`. As `total-assets-preview` (the vault's TVL, growing monotonically over time) becomes large relative to the tiny per-accrual `reserve-inc`, this integer division truncates to `0`, silently and permanently discarding the DAO treasury's reserve-factor cut of interest for that accrual — with no way to recover the lost fraction later.

### Finding Description
Every vault contract computes the protocol's reserve share of newly accrued interest and mints it as vault shares (`zft`) to `.dao-treasury` on each `accrue` call: [1](#0-0) 

```clarity
(let ((next (next-index))
      (nliq (next-liquidity-index))
      (scaled-principal (var-get principal-scaled))
      (old-debt (mul-div-down scaled-principal idx INDEX-PRECISION))
      (new-debt (mul-div-down scaled-principal next INDEX-PRECISION))
      (debt-delta (if (> new-debt old-debt) (- new-debt old-debt) u0))
      (reserve-inc (mul-div-down debt-delta (var-get fee-reserve) BPS))
      (treasury-lp (if (> reserve-inc u0) (mul-div-down reserve-inc (total-supply) (- (total-assets-preview) reserve-inc)) u0)))
```

`treasury-lp` is derived from `mul-div-down`, which floors the result: [2](#0-1) 

`reserve-inc` is itself a tiny fraction (`fee-reserve`/`BPS`, e.g. 10%) of `debt-delta`, which is the interest accrued only since the *last* `accrue` call (often a single block/transaction). As the vault's TVL (`total-assets-preview`, via `total-assets` at [3](#0-2)  which sums deposited assets plus already-accrued interest) grows into the millions of underlying-asset base units, the ratio `reserve-inc * total-supply / (total-assets-preview - reserve-inc)` shrinks toward the point where it truncates to `0` for any accrual with a small enough `debt-delta` (short time gap between transactions, or moderate utilization/rate). When `treasury-lp` is `0`, the mint is skipped entirely: [4](#0-3) 
```clarity
(if (> treasury-lp u0)
    (try! (ft-mint? zft treasury-lp .dao-treasury))
    false)
```

Crucially, `idx`/`lindex` are still advanced to `next`/`nliq` even when `treasury-lp` rounds to zero — the debt-index step (and thus the interest owed by borrowers, captured entirely for the benefit of suppliers) is permanently recorded, but the reserve factor's share of that same interest is silently dropped. There is no accumulator or remainder carried forward to a subsequent accrual, so the loss compounds with every sub-threshold accrual and is never recovered. The same pattern is identical across all vault contracts (`v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`, `v0-vault-usdc.clar`, `v0-vault-usdh.clar`), all sharing the identical `calc-treasury-lp-preview`/inline-`accrue` formula.

### Impact Explanation
This falls under the in-scope **High** impact class: *"theft of unclaimed yield ... or permanent freezing of unclaimed yield ... "*. The protocol's designed reserve-factor revenue (unclaimed yield allocated to `.dao-treasury`) is permanently and irrecoverably lost on every accrual where the rounding truncates to zero. As vaults mature and TVL grows — which is the expected steady-state for a lending protocol — small, frequent accruals (each user interaction triggers `accrue`) become increasingly likely to round the treasury's share to `0`, while the borrower-side debt index still advances normally, meaning suppliers absorb 100% of the interest that should have been split with the protocol reserve.

### Likelihood Explanation
Likelihood is high and grows over time without any attacker action required: any ordinary user calling `deposit`, `redeem`, `system-borrow`, or any market operation that triggers `accrue` on a vault with non-trivial TVL and short inter-transaction time deltas will reproduce the truncation. No privileged access or malicious intent is needed — normal usage patterns (frequent small transactions) on a vault with large `total-assets-preview` are sufficient.

### Recommendation
Track a running remainder (accumulated un-minted reserve fraction) across accruals instead of discarding sub-unit amounts each time, or switch the ratio to `mul-div-up` for the treasury's share specifically (biasing rounding in favor of the protocol rather than silently favoring suppliers), or accumulate `reserve-inc` in underlying-asset terms in a persistent variable and only convert/mint shares once the accumulated amount clears the rounding threshold.

### Proof of Concept
1. Let a vault reach `total-assets-preview` = 100,000,000 (underlying base units) and `total-supply` ≈ 100,000,000 shares (1:1 ratio for simplicity).
2. A user calls `deposit`/`redeem`/`borrow` shortly (e.g., 1 second) after the previous `accrue`, causing a small `time-delta`, hence a small `debt-delta` (e.g., a few base units of interest).
3. `reserve-inc = mul-div-down(debt-delta, fee-reserve, BPS)` — with `fee-reserve` = 1000 (10%), a `debt-delta` of, say, 5, yields `reserve-inc = (5*1000)/10000 = 0` already at the first stage for small deltas; even where `reserve-inc` is nonzero (e.g., `debt-delta`=50 → `reserve-inc`=5), `treasury-lp = mul-div-down(5, 100000000, 100000000-5)` = `5*100000000/99999995` = `5` (fine at this scale) — but as `total-assets-preview` grows into the billions of base units (e.g., BTC-denominated vaults with 8 decimals, or stacked accrued interest over years), `reserve-inc` values arising from short accrual intervals shrink below the point where the multiplication by `total-supply`/division by `total-assets-preview` floors to `0`, e.g. `reserve-inc=1, total-supply=1e11, total-assets-preview=1e13` → `treasury-lp = 1*1e11/1e13 = 0`.
4. Because `idx`/`lindex` still update at line 851/854, this loss repeats on every subsequent sub-threshold accrual, and the DAO treasury permanently forfeits its reserve-factor income for those accruals. [5](#0-4)

### Citations

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L148-149)
```text
  (/ (* x y) z))

```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L341-346)
```text
(define-private (total-assets-preview)
  (let ((current-assets (var-get assets))
        (debt (debt-preview))
        (borrowed (var-get total-borrowed))
        (interest (if (> debt borrowed) (- debt borrowed) u0)))
    (+ current-assets interest)))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L835-863)
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
