[1](#0-0) , and its mainnet counterpart [2](#0-1)  replicate the exact NotionalTradeModule bug class: an "all-positions accrual hook" that is invoked on every `borrow`/`repay`/`liquidate` call, iterates every entry in the user's collateral **and** debt lists, and `unwrap-panic`s on each vault's `accrue` call — so a revert in accrual for *any one* of the user's positions bricks *every* market operation for that user.

### Title
Per-user market operations (`borrow`/`repay`/`liquidate`) can be permanently bricked by a single failing vault `accrue()` call in the collateral/debt accrual loop - (File: `mainnet/contracts/market/v0-4-market.clar`)

### Summary
`borrow`, `repay`, and `liquidate` in `v0-4-market.clar` (and its `local-testing` equivalent `market.clar`) all begin by calling `accrue-user-debts` and/or `accrue-user-collateral` over the caller's *entire* position (all collateral and debt entries, not just the asset being operated on). Each entry is accrued via `accrue-and-cache`, whose result is consumed with `unwrap-panic` inside `accrue-debt-asset`/`accrue-collateral-asset`. If the underlying vault's `accrue` function reverts for even one of the user's other positions, the panic propagates and aborts the whole transaction — exactly the Notional `_redeemMaturedPositions` failure mode, where iterating and unconditionally trusting every external call in a hook that gates core user actions causes total lockup.

### Finding Description
`accrue-user-debts`/`accrue-debt-asset` and `accrue-user-collateral`/`accrue-collateral-asset` fold over the user's full `debt`/`collateral` lists and call: [3](#0-2) 

Both use `unwrap-panic` on `accrue-and-cache`, which in turn calls `vault-accrue` for the routed vault: [4](#0-3) 

These accrual sweeps are invoked unconditionally at the top of `borrow` and `repay`: [2](#0-1) [5](#0-4) 

and via `accrue-user-debts`/`accrue-user-collateral` in `liquidate` as well.

Each vault's `accrue()` is not a trivial getter — it performs treasury LP minting math that can revert: [6](#0-5) 

`treasury-lp` is computed as `(mul-div-down reserve-inc (total-supply) (- (total-assets-preview) reserve-inc))`. If `total-assets-preview()` ever becomes equal to or less than `reserve-inc` for a given vault (e.g. a vault whose liquid `total-assets-preview` has been driven very low relative to the accrued-interest-derived `reserve-inc`, which grows unboundedly the longer nobody calls `accrue` while `scaled-principal` and the index continue to compound), the subtraction underflows or the subsequent division divides by zero — both are Clarity runtime aborts that cannot be caught by `try!`/`unwrap-panic` and propagate as a full transaction failure.

Because this accrual sweep is executed over *every* position a user holds (not just the asset being borrowed/repaid), a single such reverting vault poisons every future `borrow`, `repay`, and `liquidate` call touching that user's position — including calls the user makes to try to fix the problem (e.g., repay the affected asset), since `repay` also runs `accrue-user-debts` over the whole debt list first. This is structurally identical to the M-06 report: an unconditional "redeem/accrue all matured/related positions" hook, gating core issuance/redemption-equivalent functions, that has no fallback when one sub-call fails.

### Impact Explanation
If any single vault's `accrue()` becomes non-reverting-unsafe (via the underflow/division-by-zero path described, or any other future revert condition in a vault's `accrue`), every user holding that asset as collateral or debt loses the ability to `borrow`, `repay`, or have their position `liquidate`d — their collateral and any yield/interest continuing to accrue on it become temporarily frozen, since the only paths to modify or unwind the position (`repay`, `collateral-remove`, `liquidate`) all route through the same all-positions accrual sweep. This lands in the in-scope "temporary freezing of funds" impact category.

### Likelihood Explanation
The trigger condition (`total-assets-preview() <= reserve-inc`) requires a specific vault-utilization state (heavy borrowing driving up `debt-delta`/`reserve-inc` relative to a depleted `total-assets-preview`) that is influenced by ordinary user borrow/repay activity and the passage of time without accrual — no privileged access or DAO action is needed to reach the reverting state, only to have accrue skipped for long enough for the numbers to diverge. Likelihood is moderate: it depends on specific fee-reserve/utilization parameters, but the underlying design flaw — trusting an unconditional loop of external `accrue` calls with `unwrap-panic` inside a function that gates all core user actions — is a structural certainty once any one accrue call can revert.

### Recommendation
- Do not `unwrap-panic` on `accrue-and-cache` results inside `accrue-debt-asset`/`accrue-collateral-asset`; propagate failures gracefully or skip the failing asset instead of aborting the whole transaction.
- Guard the `treasury-lp` calculation against `total-assets-preview() <= reserve-inc` (clamp `reserve-inc` or skip minting when the denominator would be non-positive) so `accrue()` can never revert.
- Consider decoupling "accrue this position's own asset" from "accrue and mint treasury LP for all other positions," so a user's ability to `repay`/`liquidate` their own debt does not depend on the health of unrelated vaults' accrual/minting logic.

### Proof of Concept
1. Drive a vault (e.g. `v0-vault-usdc`) into a state where `total-assets-preview()` is small relative to accumulated `reserve-inc` — e.g., heavy borrowing with a high `fee-reserve` and skip calling `accrue` for an extended period so `debt-delta` (and thus `reserve-inc`) grows large relative to the vault's `total-assets-preview`.
2. Call `accrue()` directly (or indirectly via `system-borrow`/`system-repay`) once `(- (total-assets-preview) reserve-inc)` would underflow or equal zero — the transaction aborts with a runtime arithmetic error.
3. Any market user who holds this asset as collateral or debt now has `borrow`, `repay`, and any liquidation of their position abort, because `accrue-user-debts`/`accrue-user-collateral` unconditionally `unwrap-panic`s on `accrue-and-cache` for this vault as part of processing their full position, per [1](#0-0) .

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

**File:** local-testing/contracts/market/market.clar (L267-302)
```text
(define-private (accrue-user-debts (debt-list (list 64 { aid: uint, scaled: uint})))
  (fold accrue-debt-asset debt-list { success: true }))

(define-private (accrue-debt-asset
  (debt-entry { aid: uint, scaled: uint })
  (acc { success: bool }))
  (begin
    ;; this will use cache if available, accrue if not
    (unwrap-panic (accrue-and-cache (get aid debt-entry)))
    acc))

(define-private (accrue-user-collateral (coll-list (list 64 {aid: uint, amount: uint})))
  (fold accrue-collateral-asset coll-list { success: true }))

(define-private (accrue-collateral-asset
  (coll-entry { aid: uint, amount: uint })
  (acc { success: bool }))
  (let ((aid (get aid coll-entry)))
    ;; Only accrue if asset is a registered ztoken
    (if (is-ztoken aid)
        ;; ZToken: map to underlying vault routing ID and accrue
        ;; zSTX(1)->STX(0), zsBTC(3)->sBTC(2), zstSTX(5)->stSTX(4), zUSDC(7)->USDC(6), zUSDH(9)->USDH(8), zstSTXbtc(11)->stSTXbtc(10)
        (let ((vault-id (if (is-eq aid zSTX) STX
                        (if (is-eq aid zsBTC) sBTC
                        (if (is-eq aid zstSTX) stSTX
                        (if (is-eq aid zUSDC) USDC
                        (if (is-eq aid zUSDH) USDH
                        (if (is-eq aid zstSTXbtc) stSTXbtc
                        ;; Should never reach here if is-ztoken is correct
                        ;; but if reached will cause ERR-UNKNOWN-VAULT with any value over 64
                        u100))))))))
          (begin
            (unwrap-panic (accrue-and-cache vault-id))
            acc))
        ;; Non-ztoken: skip accrual (no liquidity index needed)
        acc)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1246-1252)
```text
        ;; Step 1: Get position WITHOUT resolving prices
        (position (try! (get-position account)))
        (mask (get mask position))
        
        ;; Step 2: Accrue user's positions (populates cache for ztokens)
        (u-debt (accrue-user-debts (get debt position)))
        (u-coll (accrue-user-collateral (get collateral position)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1327-1328)
```text
        ;; Step 2: Accrue user's positions (populates cache for ztokens)
        (u-debt (accrue-user-debts (get debt position)))
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
