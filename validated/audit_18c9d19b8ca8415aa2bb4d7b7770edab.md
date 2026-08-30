Now I have enough to identify the concrete analog. Both `deposit` (mints shares from assets in) and `redeem` (returns assets from shares burned) call the same rounding-down helpers, `convert-to-shares-preview` and `convert-to-assets-preview`, both of which use `mul-div-down` unconditionally.### Title
Protocol reserve fee (treasury LP share) computed with round-down instead of round-up, systematically under-crediting the protocol on every interest accrual - ([File: mainnet/contracts/vault/v0-vault-stx.clar])

### Summary
Every vault contract (`v0-vault-stx.clar`, `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`, `v0-vault-usdc.clar`, `v0-vault-usdh.clar`) computes the protocol's interest-reserve fee and the resulting treasury LP-share mint using `mul-div-down` exclusively, with no rounding-up variant applied in favor of the protocol/fee recipient, exactly the anti-pattern flagged in the external report ("fee calculations ... currently do not round in favor of the protocol and fee recipients").

### Finding Description
Each vault defines both a round-down and round-up multiply-divide helper: [1](#0-0) 

but the treasury fee computation only ever uses the round-down variant: [2](#0-1) 

`calc-treasury-lp-preview` computes `debt-delta` (interest accrued since the last index update), then derives the protocol's cut `reserve-inc` via `mul-div-down debt-delta fee-reserve BPS`, and finally converts that fee amount into LP shares via another `mul-div-down`. Both steps truncate toward zero. Per the recommended AMM/vault best practice cited in the report, protocol/fee-recipient-owed amounts should round up (in favor of the protocol), while user-owed amounts should round down (in favor of the protocol on the other side). Here, the fee amount owed *to* the protocol is rounded down against the protocol, meaning the fraction lost to truncation accrues to depositors/borrowers instead of the treasury. This mirrors the exact class of bug described in the Sudoswap report: fee/protocol-share rounding that leaks value away from the protocol on every calculation, rather than a single one-off case — here it happens on every `accrue` cycle across all vaults, compounding over time.

The same `convert-to-shares-preview`/`convert-to-assets-preview` pair used for `deposit`/`redeem` is, by contrast, correctly protocol-favorable (round-down on `convert-to-shares` used for minting shares on deposit, and round-down on `convert-to-assets` used for paying out assets on redeem), confirming the codebase is aware of the deposit/redeem rounding-direction requirement but failed to apply the equivalent protocol-favorable rounding to the treasury fee/reserve computation.

### Impact Explanation
The treasury's reserve fee represents the protocol's unclaimed yield share of accrued interest. Systematically rounding this down instead of up means the protocol permanently forfeits a small amount of its owed yield on every single accrual across every vault (STX, sBTC, stSTX, stSTXbtc, USDC, USDH), for the lifetime of the protocol. This is a permanent, irrecoverable loss of protocol yield that compounds with usage — falling under "theft of unclaimed yield ... permanent freezing of unclaimed yield" in the impact taxonomy.

### Likelihood Explanation
The rounding error triggers on every call to `accrue` (directly or indirectly via `deposit`/`redeem`/`system-borrow`/`system-repay`, which all call `accrue` first) whenever `debt-delta > 0`, i.e. essentially every block with active borrow activity. No special conditions or privileged access are required — it is a deterministic, unprivileged, always-on leak baked into the standard interest-accrual path.

### Recommendation
Introduce and use a `mul-div-up` rounding for amounts owed to the protocol/treasury: round `reserve-inc` (and the treasury LP-share conversion derived from it) up rather than down, mirroring `mul-div-up` already defined in the same file. Apply the fix consistently across all six vault contracts (`v0-vault-stx.clar`, `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`, `v0-vault-usdc.clar`, `v0-vault-usdh.clar`), since they share the identical `calc-treasury-lp-preview` logic.

### Proof of Concept
1. Observe `debt-delta`, `fee-reserve` (bps), and `BPS` such that `debt-delta * fee-reserve` is not an exact multiple of `BPS` (the common case, since `fee-reserve` is an arbitrary bps value like e.g. 1000/10000).
2. `reserve-inc = mul-div-down(debt-delta, fee-reserve, BPS)` truncates the remainder, e.g. `debt-delta=12345`, `fee-reserve=1000`, `BPS=10000` → exact value `1234.5`, truncated to `1234`, discarding `0.5` unit of protocol fee.
3. This discarded remainder is never re-added to a future accrual (each `accrue` recomputes `debt-delta` from fresh index deltas), so the protocol loses that fraction of yield permanently on every accrual across every vault, for as long as the protocol operates.

### Citations

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L147-151)
```text
(define-private (mul-div-down (x uint) (y uint) (z uint))
  (/ (* x y) z))

(define-private (mul-div-up (x uint) (y uint) (z uint))
  (/ (+ (* x y) (- z u1)) z))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L350-360)
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
        (mul-div-down reserve-inc (total-supply) (- ta-preview reserve-inc))
```
