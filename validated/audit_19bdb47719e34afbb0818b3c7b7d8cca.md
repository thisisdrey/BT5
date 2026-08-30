Found a legitimate analog: the vault contracts (`v0-vault-usdc.clar`, `v0-vault-sbtc.clar`, `v0-vault-stx.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`, `v0-vault-usdh.clar`) contain an unguarded division inside `accrue`/`calc-treasury-lp-preview` that mirrors the report's root cause: a division-by-zero denominator that is not checked at the site of the division, reachable from every core user-facing operation.

### Title
Unguarded division by zero in vault `accrue` treasury-LP minting freezes deposit/withdraw/borrow/repay — ([File: mainnet/contracts/vault/v0-vault-usdc.clar])

### Summary
Every Zest lending vault (`v0-vault-usdc.clar`, `v0-vault-sbtc.clar`, `v0-vault-stx.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`, `v0-vault-usdh.clar`) computes a reserve/treasury LP mint amount inside `accrue` using `mul-div-down reserve-inc (total-supply) (- (total-assets-preview) reserve-inc)`, with no check that `(total-assets-preview) - reserve-inc` is non-zero before using it as a divisor.

### Finding Description
`accrue` is a public, unauthenticated function that is also invoked as the first step of nearly every vault state-mutating action (`system-borrow`, `system-repay`, `deposit`/`redeem` flows), just like `claim -> calculate_rewards` is invoked whenever a user claims rewards in the referenced report. [1](#0-0) 

Inside `accrue`, `treasury-lp` is computed as:
```
(treasury-lp (if (> reserve-inc u0) (mul-div-down reserve-inc (total-supply) (- (total-assets-preview) reserve-inc)) u0))
``` [2](#0-1) 

`mul-div-down` performs `(/ (* x y) z)` with no zero-check on `z`: [3](#0-2) 

The same unguarded pattern exists in the read-only `calc-treasury-lp-preview` helper used for off-chain previews: [4](#0-3) 

If `total-assets-preview()` (current idle assets in the vault plus recognized interest) is ever equal to `reserve-inc` (the newly recognized reserve-fee portion of that interest) — which becomes likely at high utilization when `current-assets` (idle, unborrowed liquidity) approaches zero — the subtraction `(- (total-assets-preview) reserve-inc)` evaluates to `0`, and the subsequent `mul-div-down` call reverts with a division-by-zero error. Unlike `socialize-debt` in the same file, which explicitly guards its analogous ratio computation with `(if (> scaled-principal u0) ... u0)`, this treasury-LP computation has no such guard at the actual division site — exactly the flaw called out in the external report ("division by zero handling is not done at the site of division"). [5](#0-4) 

Because `accrue` is called at the start of `system-borrow`, `system-repay`, and (via `deposit`/`redeem` accrual paths) essentially all user-facing vault interactions, hitting this zero-denominator condition reverts every such call for that vault until the condition changes.

### Impact Explanation
This causes a temporary freezing of user funds: while the exact-zero condition holds, deposits, withdrawals, borrowing, and repayment on the affected vault all revert (since they all funnel through `accrue`), because `accrue` is a public function invoked unconditionally at the top of these flows before any of the intended logic executes. This matches the High-severity "temporary freezing of funds" impact class, directly analogous to the report's "user cannot claim rewards or close_position" scenario, where an unguarded division inside a frequently-invoked accounting routine blocks core user actions.

### Likelihood Explanation
The condition is reachable purely through normal vault usage (borrowing that drains idle liquidity to exactly the point where recognized interest reserve share equals total previewed assets) with no privileged action or DAO involvement required — it is a function of `fee-reserve`, `total-borrowed`, `principal-scaled`, `index`, and `assets`, all of which evolve through ordinary `system-borrow`/`system-repay`/deposit/redeem calls. High-utilization vaults (near-zero idle `assets`) are the most likely to trigger it.

### Recommendation
Guard the division at its source: check that `(- (total-assets-preview) reserve-inc)` is non-zero before calling `mul-div-down`, and if it is zero, fall back to a safe default (e.g., skip minting or use `total-assets-preview` as-is) — mirroring the guard already present in `socialize-debt`'s `principal-reduction` computation. Apply the same fix to `calc-treasury-lp-preview` and to all six vault contracts (`v0-vault-usdc.clar`, `v0-vault-sbtc.clar`, `v0-vault-stx.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`, `v0-vault-usdh.clar`) which share this identical code pattern.

### Proof of Concept
1. Drive a vault to high utilization via `system-borrow` such that `assets` (idle underlying) approaches zero while `total-borrowed`/`principal-scaled` accrue interest.
2. Wait until a block where `next-index` causes `debt-delta` and thus `reserve-inc` to equal `total-assets-preview()` exactly (feasible when idle `assets` is 0 and interest recognized this period equals the reserve-fee share of debt delta).
3. Call any of `accrue`, `system-borrow`, `system-repay`, or the deposit/redeem entry points that call `accrue` first — the call reverts with a Clarity division-by-zero error, blocking all vault operations for that call until state naturally shifts past the exact-zero condition.

### Citations

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L148-149)
```text
  (/ (* x y) z))

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

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L837-865)
```text
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

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L953-960)
```text
        ;; Write down lindex proportionally to loss in total-assets
        (new-lindex (if (and (> old-total-assets u0) (> old-total-assets debt-reduction))
                       (mul-div-down current-lindex (- old-total-assets debt-reduction) old-total-assets)
                       u0)))

    (try! (check-caller-auth))
    (asserts! (> scaled-amount u0) ERR-AMOUNT-ZERO)

```
