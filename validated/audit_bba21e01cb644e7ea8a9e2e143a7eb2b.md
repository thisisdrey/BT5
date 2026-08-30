### Title
`accrue()` computes reserve/treasury shares with `total-assets-preview()` as denominator, without protecting the subtraction from underflow, allowing an unbounded-revert DoS analogous to FighterFarm's unguarded mint path - (File: `mainnet/contracts/vault/v0-vault-usdc.clar` and equivalent vaults)

### Summary
The C4 AI-Arena mitigation finding concerns `FighterFarm.mintFromMergingPool()`: a state-mutating call reachable from ordinary flows that lacked a defensive bound, so a single crafted/edge-case input could make the mint permanently revert and block the shared code path for all subsequent callers. The Zest analog is the `treasury-lp` computation inside every vault's `accrue()`, which divides by `(- (total-assets-preview) reserve-inc)` without checking that `total-assets-preview` is strictly greater than `reserve-inc`. Because `accrue()` is invoked at the top of `deposit`, `redeem`, `system-borrow`, and `system-repay`, an underflow there aborts the entire transaction for every caller, not just the one who triggered the edge case.

### Finding Description
`accrue()` in the vault contracts computes: [1](#0-0) 
```
(reserve-inc (mul-div-down debt-delta (var-get fee-reserve) BPS))
(treasury-lp (if (> reserve-inc u0) (mul-div-down reserve-inc (total-supply) (- (total-assets-preview) reserve-inc)) u0))
```
and the read-only preview helper `calc-treasury-lp-preview` performs the identical unguarded subtraction: [2](#0-1) 

`total-assets-preview` is defined as `current-assets (var assets) + max(debt-preview - total-borrowed, 0)`: [3](#0-2) 

Unlike similar Clarity arithmetic elsewhere in the same file which is defensively clamped (e.g. `(if (> debt borrowed) (- debt borrowed) u0)`), the subtraction `(- (total-assets-preview) reserve-inc)` has **no such guard**. In Clarity, `uint` subtraction that would go negative aborts the transaction (runtime error), not merely returns an error response — there is no `asserts!`/`try!` wrapping that would let the caller catch and recover from it.

`reserve-inc` scales with `debt-delta` (the newly accrued interest since `last-update`) multiplied by `fee-reserve`/`BPS`, while `total-assets-preview` is the vault's tracked `assets` plus outstanding accrued interest. Under intended parameters `reserve-inc` should always be a small fraction of `total-assets-preview`. However, this relationship is never explicitly asserted on-chain; it depends on: (1) `assets` staying non-trivial relative to accrued debt-delta, and (2) `fee-reserve` being bounded to a sane value. If either external assumption is violated — e.g. `assets` is driven very low via legitimate `redeem` calls that empty the vault's tracked assets down toward the outstanding-debt floor while interest continues to accrue on a large `principal-scaled`, or if a long dormant period allows `debt-delta` to grow very large relative to remaining `assets` — `reserve-inc` can meet or exceed `total-assets-preview`, causing the subtraction to underflow and abort every call that reaches `accrue()`.

Once this state is reached, `accrue()` reverts every time it is invoked (the underflow condition is a function of on-chain state, `index`/`fee-reserve`, and elapsed time — it does not require any privileged input, so it is fully reachable from ordinary `deposit`/`redeem`/borrow flows), and because `deposit`, `redeem`, `system-borrow`, and `system-repay` all call `(try! (accrue))` first, the entire vault becomes permanently unusable — this is structurally the same "reachable-but-unguarded mint/mutation blocks all future callers" pattern as the FighterFarm analog.

### Impact Explanation
This lands in the "temporary/permanent freezing of funds" impact class: once the underflow condition is hit, `accrue()` unconditionally reverts, and since every vault entry point (`deposit`, `redeem`, `system-borrow`, `system-repay`) requires a successful `accrue()` first, all supplied principal and collateral routed through that vault become frozen with no code path to recover (there is no separate "reset" mechanism for this specific state variable relationship). This matches the in-scope "vault share math and interest accrual" component named in scope for this analysis.

### Likelihood Explanation
Likelihood is limited by how far `reserve-inc` can realistically be pushed relative to `total-assets-preview` under normal `fee-reserve` settings — the DAO controls `fee-reserve` and typical values keep it a small percentage. However, the underflow is not defended against at all in code (no `asserts!`/clamp), unlike the analogous defensive clamp used two lines above for `debt-delta`. Given that `assets` can be driven down through ordinary large `redeem` calls while debt/interest continues accruing on `principal-scaled`, and elapsed-time-driven index growth is unbounded, the arithmetic invariant `total-assets-preview > reserve-inc` is not something ordinary callers can be assumed to preserve indefinitely, especially in low-liquidity or long-idle vault states.

### Recommendation
Add the same defensive pattern used elsewhere in `accrue()`/`total-assets`: clamp the divisor with a floor check before subtracting, e.g. compute `treasury-lp` only when `total-assets-preview > reserve-inc`, and treat the reserve increment as `0` (or cap it at `total-assets-preview`) otherwise, mirroring `(if (> debt borrowed) (- debt borrowed) u0)`. Apply the same fix to `calc-treasury-lp-preview` and to all six vault contracts (`v0-vault-usdc.clar`, `v0-vault-usdh.clar`, `v0-vault-sbtc.clar`, `v0-vault-stx.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`), which all share the identical unguarded arithmetic.

### Proof of Concept
1. Vault starts with `assets = A`, `principal-scaled` representing outstanding debt, `index = I0`, `fee-reserve = F`.
2. Over time (or through repeated legitimate borrow/redeem cycles that shrink `assets` toward the vault's tracked debt floor while interest keeps accruing on `principal-scaled`), `debt-delta` computed in `accrue()` grows large relative to `assets`, such that `reserve-inc = debt-delta * F / BPS` approaches or exceeds `total-assets-preview = assets + max(debt-preview - total-borrowed, 0)`.
3. Any ordinary user calls `deposit`, `redeem`, `system-borrow`, or `system-repay`, which call `(try! (accrue))` at line ~845 in `mainnet/contracts/vault/v0-vault-usdc.clar`.
4. `(- (total-assets-preview) reserve-inc)` underflows a `uint`, aborting the transaction — no `try!`/`asserts!` can be used to gracefully surface this since it's a raw runtime arithmetic failure, not an `(err ...)` response.
5. Because every state-mutating vault entry point depends on `accrue()` succeeding, the vault becomes permanently frozen for all depositors and borrowers, mirroring the "single unguarded reachable operation blocks all future callers" bug class from the FighterFarm report.

Note: Verifying the precise numeric conditions under which `reserve-inc` can practically reach `total-assets-preview` (i.e., how large `fee-reserve` and `debt-delta` can realistically get under DAO-approved parameters and typical usage) would require deeper analysis of `fee-reserve` governance bounds and the interest-rate curve (`interpolate-rate`/`calc-multiplier-delta`), which I was not able to fully trace within the available context.

### Citations

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L339-344)
```text
(define-private (total-assets-preview)
  (let ((current-assets (var-get assets))
        (debt (debt-preview))
        (borrowed (var-get total-borrowed))
        (interest (if (> debt borrowed) (- debt borrowed) u0)))
    (+ current-assets interest)))
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L348-359)
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
        u0)))
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L845-852)
```text
                (new-debt (mul-div-down scaled-principal next INDEX-PRECISION))
                (debt-delta (if (> new-debt old-debt) (- new-debt old-debt) u0))
                (reserve-inc (mul-div-down debt-delta (var-get fee-reserve) BPS))
                (treasury-lp (if (> reserve-inc u0) (mul-div-down reserve-inc (total-supply) (- (total-assets-preview) reserve-inc)) u0)))
            (if (not (is-eq idx next))
                (var-set index next)
                false)
            (if (not (is-eq lidx nliq))
```
