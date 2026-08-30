### Title
Treasury Reserve-Fee Rounds to Zero via Truncating Division, Letting Attackers Deny Protocol Its Interest Cut - ([File: mainnet/contracts/vault/v0-vault-usdc.clar])

### Summary
Every Zest vault contract (`v0-vault-usdc.clar`, `v0-vault-usdh.clar`, `v0-vault-stx.clar`, `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`) computes the DAO treasury's share of newly accrued interest with a round-down integer division (`mul-div-down`). Because `accrue()` is a public, unauthenticated entry point that any principal can call at will, an attacker can force accrual on tiny debt deltas so the reserve-fee calculation truncates to zero every time, permanently denying the treasury its cut of interest that borrowers still pay in full to lenders.

### Finding Description
In `accrue()`, the treasury's reserve-fee mint amount is derived as: [1](#0-0) 

```
(old-debt (mul-div-down scaled-principal idx INDEX-PRECISION))
(new-debt (mul-div-down scaled-principal next INDEX-PRECISION))
(debt-delta (if (> new-debt old-debt) (- new-debt old-debt) u0))
(reserve-inc (mul-div-down debt-delta (var-get fee-reserve) BPS))
(treasury-lp (if (> reserve-inc u0) (mul-div-down reserve-inc (total-supply) (- (total-assets-preview) reserve-inc)) u0))
```

`mul-div-down` is defined as plain truncating division: [2](#0-1) 

Whenever `debt-delta * fee-reserve / BPS` truncates to `0`, `treasury-lp` is forced to `0` and the `ft-mint?` to `.dao-treasury` is skipped entirely — yet the full `debt-delta` is still applied to `index`, so lenders still receive 100% of the accrued interest via the index update. The treasury's reserve-fee share for that accrual window is simply lost, not merely deferred, because each subsequent `accrue()` call recomputes `debt-delta` against the *new* baseline `idx`, resetting the window.

`accrue()` has no access control (`(define-public (accrue) ...)`) and is also invoked internally by `deposit`, `redeem`, `system-borrow`, `system-repay`, all of which are reachable by ordinary principals: [3](#0-2) 

An attacker who opens a small borrow position (via a normal `system-borrow`-triggering flow) and then repeatedly calls any accrue-triggering action across successive blocks keeps `debt-delta` (and therefore `reserve-inc`) below the truncation threshold on every call, since each call resets `last-update` and shrinks the interest window back to a fresh tiny delta. This mirrors the reported bug class exactly: a percentage-fee computed via truncating integer division that reliably rounds to zero for attacker-chosen small inputs, letting the attacker's activity permanently escape the fee.

### Impact Explanation
This falls under "theft of unclaimed yield": the reserve-fee is the protocol's designed cut of interest revenue (`fee-reserve` in BPS) that would otherwise be minted to `.dao-treasury` as `zft` shares. By keeping each accrual window's `debt-delta` below the rounding floor, the attacker's borrow position never contributes its reserve-fee share to the treasury over its entire lifetime, even though the position still accrues and eventually pays full interest to depositors. This is unclaimed protocol yield that is permanently and unrecoverably lost, since the computation window resets on every accrual and previously-truncated remainders are never carried forward or reconciled.

### Likelihood Explanation
- `accrue()` requires no privilege and no special conditions — any principal (or a simple bot) can trigger it repeatedly.
- The magnitude threshold to trigger truncation (`debt-delta * fee-reserve < BPS`) is easily satisfiable by borrowing a small principal and calling accrue frequently across consecutive blocks, since interest accrued per short interval on a small position is naturally sub-threshold.
- The same flawed pattern is duplicated identically across all six production vault contracts, increasing the attack surface.

### Recommendation
Round the reserve-fee calculation up (or accumulate a persisted remainder across accrual calls) instead of truncating down, e.g.:
```
(reserve-inc (mul-div-up debt-delta (var-get fee-reserve) BPS))
```
Alternatively, track unminted reserve-fee remainder in contract state and carry it forward into the next accrual's `reserve-inc` computation so no accrual window can permanently zero out the protocol's yield share, regardless of how frequently `accrue()` is invoked.

### Proof of Concept
1. Attacker (or colluding account) opens a small `system-borrow` position on a vault (e.g., `v0-vault-usdc.clar`) so that `principal-scaled` is small.
2. Attacker calls any accrue-triggering public function (`deposit`, `redeem`, or directly relies on other users' transactions) once per block for many consecutive blocks.
3. On each call, `next-index` advances only slightly (`time-delta` is small), so `debt-delta = new-debt - old-debt` computed against the just-updated `idx` baseline is small enough that `(mul-div-down debt-delta fee-reserve BPS)` truncates to `0`.
4. `treasury-lp` is `0` on every call, so `ft-mint? zft treasury-lp .dao-treasury` never executes, while `index` still advances by the full amount, meaning depositors still earn the full un-taxed interest.
5. Compare against calling `accrue()` once after the same total elapsed time: `debt-delta` would be large enough to produce a nonzero `reserve-inc`, showing the treasury's fee share is lost purely due to call frequency/timing chosen by the attacker, not due to any change in total interest paid.

### Citations

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L167-168)
```text
        u0
        (mul-div-down debt-amount BPS total))))
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L765-772)
```text
      (account contract-caller)
      (CAP-SUPPLY (var-get cap-supply))
      (current-assets (var-get assets))
      (inkind (convert-to-shares-preview amount)))

    (asserts! (not (get deposit states)) ERR-PAUSED)
    (asserts! (var-get initialized) ERR-INIT)
    (asserts! (not (var-get in-flashloan)) ERR-REENTRANCY)
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L841-861)
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
