Based on my analysis, the vault's `pause-states.repay` flag and the market's liquidation grace-period mechanism are decoupled, creating exactly the reported bug class through a different but stronger path than the report describes.

### Title
Grace-period protection for liquidation resumption does not cover the vault-level `repay` pause used internally by `liquidate`, allowing immediate liquidation after unpausing repay - ([File: mainnet/contracts/vault/v0-vault-usdc.clar])

### Summary
`liquidate()` in the market contract settles debt by calling `vault-system-repay`, which invokes each vault's `system-repay` function. `system-repay` enforces `(asserts! (not (get repay states)) ERR-PAUSED)` [1](#0-0)  — the exact same `repay` flag inside `pause-states` that blocks ordinary user `repay()` calls [2](#0-1) . This means pausing vault `repay` (a normal operational action, separate from the market's dedicated `pause-liquidation` mechanism) also silently blocks all liquidations for that asset, since liquidation calls the identically-gated `system-repay`.

### Finding Description
The market contract has a purpose-built grace-period system to solve exactly the reported bug: when `set-pause-liquidation` transitions from paused to unpaused, it sets a grace-end timestamp so `is-liquidation-paused` keeps blocking liquidations for a cooldown window [3](#0-2) [4](#0-3) .

However, this grace period is only armed by the market's own `set-pause-liquidation`/`pause-liquidation` variable. It is never consulted or updated by the vault's independent `pause-states` map, even though `liquidate()`'s debt settlement step depends on the vault's `repay` flag being unset (`vault-system-repay` → `system-repay` → `asserts! (not (get repay states))`) [5](#0-4) . So an operator pausing only the vault's `repay` flag (e.g., during an oracle incident or migration, without separately calling `set-pause-liquidation`) inadvertently also disables liquidation for that asset the whole time repay is paused, but does so through a path that has zero grace-period protection. When the vault's `repay` flag is later cleared, liquidation resumes immediately (the market's `pause-liquidation`/grace-period state was never touched), letting bots liquidate positions that drifted unhealthy during the pause window with no cooldown — reproducing the exact scenario in the report, but via a mechanism the grace-period fix does not cover.

### Impact Explanation
Borrowers whose positions became unhealthy purely due to price movement during the vault `repay` pause are liquidated the instant the vault unpauses `repay`, with no opportunity to restore health by repaying (which was blocked) or by front-running bots. This is a temporary/permanent freezing-and-loss-of-funds impact for affected borrowers (loss of collateral beyond what pure market movement should have caused), matching the "temporary freezing of funds" / unfair-liquidation impact class the report itself targets.

### Likelihood Explanation
This requires only an ordinary, non-compromised operational action (pausing the vault's `repay` state via the standard DAO-controlled pause lever) followed by a normal unpause — not a DAO compromise or registry misconfiguration. Given the market's own dedicated grace-period feature exists specifically to prevent instant post-pause liquidations, it is a realistic operational mistake to rely on that feature while not realizing the vault-level `repay` pause bypasses it entirely.

### Recommendation
Couple the vault's `repay` pause/unpause transition to the market's liquidation grace-period logic (e.g., have the vault's pause setter call into the market to arm `liquidation-grace-periods` for the corresponding asset ID whenever `repay` transitions from paused to unpaused), or have `is-liquidation-paused` also treat "vault repay currently paused" as an automatic liquidation pause with its own grace-period on unpause.

### Proof of Concept
1. DAO/operator calls the vault's pause-setting function to set `pause-states.repay = true` for an asset (without calling market's `set-pause-liquidation`).
2. During this window, market price moves such that some borrower's position becomes unhealthy; the borrower cannot call `repay()` to restore health because `system-repay` reverts with `ERR-PAUSED`.
3. Liquidation calls also fail during this window because `liquidate()`'s internal `vault-system-repay` hits the same `ERR-PAUSED` check — masking the fact that positions are drifting unhealthy with no safety net.
4. DAO/operator clears `pause-states.repay = false`. The market's `pause-liquidation` variable and `liquidation-grace-periods` map were never touched, so `is-liquidation-paused` returns `false` immediately [4](#0-3) .
5. A liquidation bot immediately calls `liquidate()` on the now-solvent-blocked borrower's position in the same block/transaction that repay resumes, seizing collateral with no grace period, exactly as described in the external report.

### Citations

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L98-115)
```text
;; -- Pause states
(define-data-var pause-states
  {
    deposit: bool,
    redeem: bool,
    borrow: bool,
    repay: bool,
    accrue: bool,
    flashloan: bool
  }
  {
    deposit: false,
    redeem: false,
    borrow: false,
    repay: false,
    accrue: false,
    flashloan: false
  })
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L900-923)
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

    (try! (receive-underlying capped-amount tx-sender))
    (var-set principal-scaled updated-scaled-principal)
    (var-set total-borrowed total-borrowed-new)
    (var-set assets (+ (var-get assets) interest-paid))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L691-697)
```text
(define-private (is-liquidation-paused (asset-id uint))
  (let ((manual-pause (var-get pause-liquidation))
        (global-grace-end (default-to u0 (map-get? liquidation-grace-periods GLOBAL-LIQUIDATION-GRACE-ID)))
        (asset-grace-end (default-to u0 (map-get? liquidation-grace-periods asset-id)))
        (global-grace-active (< stacks-block-time global-grace-end))
        (asset-grace-active (< stacks-block-time asset-grace-end)))
    (or manual-pause global-grace-active asset-grace-active)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L953-976)
```text
(define-public (set-pause-liquidation (paused bool) (grace-period uint))
  (begin
    (try! (check-dao-auth))
    (let ((was-paused (var-get pause-liquidation)))
      (var-set pause-liquidation paused)
      ;; Only set grace period if liquidations were paused AND now unpausing
      (if (and was-paused (not paused))
          (map-set liquidation-grace-periods GLOBAL-LIQUIDATION-GRACE-ID (+ stacks-block-time grace-period))
          false)
      
      (print {
        action: "market-set-pause-liquidation",
        caller: tx-sender,
        data: {
          was-paused: was-paused,
          now-paused: paused,
          grace-period: grace-period,
          grace-end: (if (and was-paused (not paused))
                         (+ stacks-block-time grace-period)
                         u0)
        }
      })
      
      (ok true))))
```
