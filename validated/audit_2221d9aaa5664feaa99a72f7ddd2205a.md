## Analog Found

### Title
Self-Inflicted Bad-Debt Socialization Lets a Liquidator Instantly Devalue Other Users' zToken Collateral Within the Same Block, Tipping Them Into Liquidation - ([File: local-testing/contracts/market/market.clar], [File: local-testing/contracts/vault/vault-usdc.clar])

### Summary
`liquidate()` can trigger `socialize-debt-asset`, which writes down a vault's shared `lindex` (liquidity index) when a borrower is left with no collateral. This global index is what every other user's zToken collateral is priced against for the *rest of the block* (and is immediately re-cached), so an attacker can engineer a self-liquidation that forces the write-down and then, in the same transaction, liquidate other users whose zToken-collateralized positions only became unhealthy because of that write-down — mirroring the `configure_collection` bug class where an aggregate/global weight update unexpectedly "tips" other principals' health/threshold checks.

### Finding Description
`socialize-debt-asset` calls `vault-socialize-debt` (routed to the vault's public `socialize-debt`) and then immediately refreshes the shared block-scoped `index-cache` with the vault's new (lower) `lindex`: [1](#0-0) 

The vault's `socialize-debt` writes down `lindex` proportionally to the vault's `total-assets`, which is a value shared by every holder of that vault's zToken: [2](#0-1) 

That discounted `lindex` is served from the per-block `index-cache` (keyed only by `{timestamp, aid}`, i.e. shared across *all* callers in the block) to any later notional-value computation in the same block: [3](#0-2) 

`calculate-asset-notional-value` prices zToken collateral using exactly this cached index when evaluating any user's position: [4](#0-3) 

The `liquidate` entry point is callable by any unprivileged principal, and the only same-block protection is against the *same borrower* re-liquidating themselves the same block they borrowed (`last-borrow-block`) — there is no protection preventing one liquidation's bad-debt write-down from being weaponized against a *different* borrower's position later in the same transaction/block: [5](#0-4) 

This is structurally identical to the `configure_collection` bug: a permissionless action recomputes/updates a shared aggregate value (`max_voter_weight` there, vault `lindex` here) that other principals' pass/fail checks depend on (tipping a proposal there, tipping a position's health there), with no barrier preventing the same caller from immediately exploiting the newly-updated shared value against an unrelated target in the same call sequence.

### Impact Explanation
An attacker can force a vault's zToken price down mid-block and then immediately liquidate other users' positions collateralized by that same zToken who were healthy before the write-down — this is direct theft of victim collateral/funds via liquidation penalty seizure that would not otherwise have occurred, matching the Critical impact class (direct theft of user funds at rest).

### Likelihood Explanation
Exploitation requires the attacker to open and then self-liquidate a debt position with zero residual collateral (fully feasible for any unprivileged principal — no special permission required), and to control enough scaled debt relative to `old-total-assets` in the target vault to produce a meaningful `lindex` decrease. This is more practical against vaults with lower total assets/liquidity, and can be amplified by combining with a flash loan purely as capital (permitted per scope) to size the attacker's own debt position without tying up capital long-term.

### Recommendation
Do not allow a single transaction/block to both (a) trigger bad-debt socialization for a vault and (b) liquidate other unrelated positions relying on that vault's index in the same block; alternatively, snapshot/lock the `index-cache` value used for health checks at the start of the transaction bundle, or require a cool-down before a freshly-written-down `lindex` can be used to justify liquidating a different borrower.

### Proof of Concept
1. Attacker deposits a small amount of collateral (asset A) and borrows debt (asset D, e.g. USDC) from `market.clar`, leaving position deliberately thin.
2. Price of asset A moves (or attacker times naturally) so the position becomes liquidatable with zero collateral remaining after liquidation.
3. Attacker (as `liquidator`) calls `liquidate` on their own position; `no-collateral-left` is true, triggering `socialize-debt-asset` → vault `socialize-debt`, writing down `lindex` for asset D's vault and refreshing `index-cache` for the current `stacks-block-time`.
4. In the same transaction (attacker's own contract orchestrating both calls), attacker calls `liquidate` again against a victim borrower whose collateral is zToken-D (zUSDC), whose notional value is now computed off the discounted cached `lindex`, pushing the victim's LTV over `ltv-liq-partial` when it was not before step 3.
5. Attacker seizes victim's collateral plus liquidation penalty, profit realized within one atomic transaction.

### Citations

**File:** local-testing/contracts/market/market.clar (L253-265)
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

**File:** local-testing/contracts/market/market.clar (L566-596)
```text
(define-private (calculate-asset-notional-value
          (asset-entry {
              id: uint, addr: principal, decimals: uint,
              oracle: { type: (buff 1), ident: (buff 32), callcode: (optional (buff 1)), max-staleness: uint },
              collateral: bool, debt: bool, price: uint })
          (acc { clist: (list 64 { aid: uint, amount: uint }),
                  dlist: (list 64 { aid: uint, scaled: uint }),
                  coll-total: uint,
                  debt-total: uint }))
  (let ((asset-id (get id asset-entry))
        (price (get price asset-entry))
        (decimals (get decimals asset-entry))
        (collateral-list (get clist acc))
        (debt-list (get dlist acc))
        (coll-amount (find-collateral-amount collateral-list asset-id))
        (coll-notional (if (> coll-amount u0)
                           (normalize (* coll-amount price) decimals false)
                           u0))

        (debt-scaled   (find-debt-scaled debt-list asset-id))
        (debt-notional (if (> debt-scaled u0) ;; use cache instead here
                           (let ((cached (unwrap-panic (accrue-and-cache asset-id)))
                                 (ib (get index cached))
                                 (actual (mul-div-up debt-scaled ib INDEX-PRECISION)))
                             (normalize (* actual price) decimals true))
                           u0)))

    { clist: collateral-list,
      dlist: debt-list,
      coll-total: (+ (get coll-total acc) coll-notional),
      debt-total: (+ (get debt-total acc) debt-notional) }))
```

**File:** local-testing/contracts/market/market.clar (L901-925)
```text
(define-private (socialize-debt-asset
                (debt-entry { aid: uint, scaled: uint })
                (acc { borrower: principal, success: bool }))
  ;; Early return if previous socialization failed
  (if (not (get success acc))
      acc
      (let ((borrower (get borrower acc))
            (failed-status { borrower: borrower, success: false })
            (asset-id (get aid debt-entry))
            (scaled-debt (get scaled debt-entry)))

            ;; Socialize in vault - pass scaled directly to avoid rounding
            (unwrap! (vault-socialize-debt asset-id scaled-debt) failed-status)
            ;; Refresh cache with new indexes post-write-down (lindex decreased)
            (map-set index-cache
                     { timestamp: stacks-block-time, aid: asset-id }
                     (unwrap! (vault-accrue asset-id) failed-status))
            ;; Remove from obligation
            (unwrap! (contract-call? .market-vault
                                      debt-remove-scaled
                                      borrower
                                      scaled-debt
                                      asset-id) failed-status)
          acc)
        ))
```

**File:** local-testing/contracts/market/market.clar (L1451-1458)
```text
    ;; Oracle frontrunning protection: prevent same-block liquidation
    ;; This blocks flash-loan based attacks where user borrows + gets liquidated in same block
    (last-borrow-block (get last-borrow-block position))
    (same-block-check (asserts! (not (is-eq last-borrow-block stacks-block-height)) ERR-LIQUIDATION-BORROW-SAME-BLOCK))

    ;; health check (FAIL-FAST) 
    ;; Check position is liquidatable BEFORE calling calc-liq-factor
    (health-check  (asserts! (>= current-ltv ltv-liq-partial) ERR-HEALTHY))
```

**File:** local-testing/contracts/vault/vault-usdc.clar (L946-969)
```text
(define-public (socialize-debt (scaled-amount uint))
  (let ((scaled-principal (var-get principal-scaled))
        (borrowed (var-get total-borrowed))
        (idx (var-get index))
        (current-assets (var-get assets))
        (current-lindex (var-get lindex))
        (old-total-assets (total-assets))
        (debt-reduction (mul-div-down scaled-amount idx INDEX-PRECISION))
        (principal-reduction (if (> scaled-principal u0)
                                (mul-div-down scaled-amount borrowed scaled-principal)
                                u0))
        ;; Write down lindex proportionally to loss in total-assets
        (new-lindex (if (and (> old-total-assets u0) (> old-total-assets debt-reduction))
                       (mul-div-down current-lindex (- old-total-assets debt-reduction) old-total-assets)
                       u0)))

    (try! (check-caller-auth))
    (asserts! (> scaled-amount u0) ERR-AMOUNT-ZERO)

    (var-set lindex new-lindex)
    (var-set principal-scaled (if (> scaled-principal scaled-amount) (- scaled-principal scaled-amount) u0))
    (var-set total-borrowed (if (> borrowed principal-reduction) (- borrowed principal-reduction) u0))
    (var-set assets (if (> current-assets principal-reduction) (- current-assets principal-reduction) u0))

```
