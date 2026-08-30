### Title
Interest continues to accrue while repayment is paused, forcing borrowers into liquidation - ([File: mainnet/contracts/vault/v0-vault-stx.clar] and equivalent vault contracts)

### Summary
Each Zest lending vault (`v0-vault-stx.clar`, `v0-vault-sbtc.clar`, `v0-vault-usdc.clar`, `v0-vault-usdh.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`) exposes independent pause flags for `deposit`, `redeem`, `borrow`, `repay`, and `accrue`. The DAO can pause `repay` alone while leaving `accrue` active, in which case borrower debt continues to compound via the interest index while borrowers are blocked from repaying, reproducing the reported "interest accrual whilst paused" issue and pushing borrowers toward forced liquidation.

### Finding Description
The vault's pause state is a single record with independently settable flags: [1](#0-0) 

`system-repay` is gated only by the `repay` flag: [2](#0-1) 

Meanwhile, `accrue` runs unconditionally at the start of nearly every state-changing entry point (`deposit`, `transfer`, `system-borrow`, `system-repay`, etc.) as long as its own dedicated `accrue` pause flag is not set, and it advances the debt index (`next-index`) purely as a function of elapsed time and utilization/reserve ratio: [3](#0-2) 

`set-pause-states` explicitly forces an `accrue` call only when the `accrue` flag itself transitions from unpaused to paused (to capture pending interest before freezing the index), and resets `last-update` only when `accrue` transitions from paused to unpaused: [4](#0-3) 

There is no coupling between the `repay` flag and the `accrue` flag. If the DAO pauses `repay` without also pausing `accrue` (e.g., in response to a market event, oracle issue, or emergency), any other unpaused action (a third party's `deposit`, `system-borrow`, or `transfer`) — or simply the passage of time reflected the next time any unpaused entry point is invoked — continues to advance `index`/`lindex` via `next-index`/`next-liquidity-index`, growing every borrower's `total-debt` while `system-repay` remains hard-blocked by `ERR-PAUSED`. This is exactly the bug class described in the external report: interest is not required to be frozen as a precondition of, or in lockstep with, pausing repayment.

### Impact Explanation
A borrower who is at or near the liquidation threshold when the DAO pauses `repay` (leaving `accrue` active) cannot repay debt during the pause window while the debt continues to grow. Once the vault is unpaused (or once a liquidator acts, since liquidation-related debt/collateral operations in `v0-market-vault.clar` are gated by separate `debt-remove`/`collateral-remove` flags, not by the vault's `repay` flag), the borrower can be liquidated for a position that would not have been liquidatable had they been able to repay when they intended to. This results in temporary freezing of the borrower's ability to service debt and can culminate in loss of their collateral — a Critical/High impact (theft of user funds via forced liquidation / temporary freezing of funds).

### Likelihood Explanation
This requires only a normal, legitimate operational DAO action — pausing `repay` on a vault (e.g., during a market stress event) without simultaneously pausing `accrue` — which is a plausible, non-malicious operational scenario the pause-state design already anticipates handling for the `accrue` flag alone (see the special-cased `accrue`-pause logic in `set-pause-states`), but does not extend to `repay`. Any borrower who is close to their liquidation threshold at that moment is affected without needing a malicious actor.

### Recommendation
Couple the `repay` pause with interest accrual: either automatically freeze `accrue` (or otherwise stop index growth) whenever `repay` is paused, or block any index update from running while `repay` is paused, mirroring the special handling already implemented for the `accrue` flag in `set-pause-states`. As recommended in the original report, avoid making interest-index freezing depend on being a precondition for the pause action succeeding.

### Proof of Concept
1. Borrower deposits collateral and calls `system-borrow`, taking on debt tracked via `principal-scaled` and `index`.
2. DAO calls `set-pause-states` with `repay: true` but `accrue: false` (all other flags unchanged) on the relevant vault (e.g., `v0-vault-stx.clar`).
3. Any other unpaused interaction with the vault (another user's `deposit`, `system-borrow`, or `transfer`) triggers `accrue`, which advances `index`/`lindex` per `next-index`/`next-liquidity-index`, increasing the borrower's `total-debt`.
4. The borrower attempts `system-repay` and is reverted with `ERR-PAUSED` (line 919 of `v0-vault-stx.clar`) despite wanting to repay.
5. Debt continues to grow across the pause window; once conditions allow liquidation (via `v0-market-vault.clar`'s `debt-remove-scaled`/`collateral-remove`, which are governed by separate flags), the borrower is liquidated for a shortfall that accrued purely because they were blocked from repaying.

### Citations

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L723-738)
```text
(define-public (set-pause-states (states {deposit: bool, redeem: bool, borrow: bool, repay: bool, accrue: bool, flashloan: bool}))
  (begin
    (try! (check-dao-auth))
    (let ((current (var-get pause-states))
          (was-paused (get accrue current))
          (now-paused (get accrue states)))
      ;; When pausing accrue, accrue first to capture pending interest
      (if (and (not was-paused) now-paused)
          (begin (try! (accrue)) false)
          false)
      ;; When unpausing accrue, jump last-update to now to skip paused period
      (if (and was-paused (not now-paused))
          (var-set last-update stacks-block-time)
          false)
      (var-set pause-states states)
      
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

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L902-920)
```text
(define-public (system-repay (amount uint))
  (let (
        (states (var-get pause-states))
        (u (try! (accrue)))
        (scaled-principal (var-get principal-scaled))
        (idx (var-get index))
        (debt (total-debt))
        (total-borrowed-amount (var-get total-borrowed))
        (capped-amount (if (> amount debt) debt amount))
        (principal-reduction (calc-principal-ratio-reduction capped-amount scaled-principal debt))
        (capped-reduction (if (> principal-reduction scaled-principal) scaled-principal principal-reduction))
        (updated-scaled-principal (- scaled-principal capped-reduction))
        (principal-repaid (mul-div-down capped-amount total-borrowed-amount debt))
        (interest-paid (- capped-amount principal-repaid))
        (total-borrowed-new (if (> total-borrowed-amount principal-repaid) (- total-borrowed-amount principal-repaid) u0)))

    (try! (check-caller-auth))
    (asserts! (not (get repay states)) ERR-PAUSED)
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)
```
