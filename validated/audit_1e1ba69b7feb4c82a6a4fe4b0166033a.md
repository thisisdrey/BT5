### Title
`get-assets` reverts the entire transaction if any single collateral/debt asset's oracle fails, freezing borrow/repay/withdraw/liquidate for unrelated assets - ([File: mainnet/contracts/market/v0-4-market.clar])

### Summary
`get-assets` batches price resolution for every enabled collateral/debt asset in a user's mask via `price-multi-resolve`, then unconditionally `unwrap-panic`s the result. Any single oracle failure among the batch (stale feed, missing feed, confidence check failure, DIA/Pyth error, etc.) aborts the entire call, exactly mirroring the reported `Voter.distribute` pattern where one bad item in a loop makes the whole batch operation fail. Here the "gauges" are the user's active assets, and `get-assets` is invoked from `borrow`, `collateral-remove`, `liquidate`, and other core flows.

### Finding Description
`get-assets` resolves prices for all assets implied by a position's bitmap in one shot: [1](#0-0) 

```
(define-private (get-assets (mask-user uint))
  (let ((mask-enabled (get-enabled-bitmap))
        (safe-mask (user-safe-mask mask-user mask-enabled))
        (iter (mask-to-list-collateral safe-mask))
        (assets-list (get-status-multi iter))
        (oracles-list (map get-oracle assets-list))
        (asset-ids (map get-asset-id assets-list))
        (prices-list (unwrap-panic (price-multi-resolve oracles-list asset-ids))))
    (map merge-price assets-list prices-list)))
```

`price-multi-resolve` folds over all oracle identifiers for the position and, per the docs, resolves each one (Pyth/DIA/callcode transforms) with staleness checks: [2](#0-1) 

Because the aggregate result is wrapped in `unwrap-panic`, a failure to resolve *any single asset's* price (e.g., one collateral type's feed goes stale, a DIA identifier errors, or confidence-ratio validation fails for just one of several assets in the mask) causes the entire `get-assets` call - and therefore the entire enclosing transaction - to abort. This is invoked from the market's primary user-facing entry points:

- `collateral-remove`: `(assets (get-assets position-mask))` [3](#0-2) 
- `borrow`: `(assets (get-assets mask))` [4](#0-3) 
- `liquidate`: `(assets (get-assets mask))` [5](#0-4) 
- `collateral-add` (new-collateral capacity check path): `(current-assets (get-assets current-mask))` [6](#0-5) 

This is the direct structural analog of the `Voter.distribute` report: a single unrelated element in an iterated batch (one gauge / one asset's oracle) can revert an entire multi-item operation that legitimate, unrelated principals depend on. In the Voter case it blocked reward distribution to all gauges; here it blocks `borrow`, `collateral-remove`, and — critically — `liquidate` for any user whose position mask happens to include the one broken/stale asset, even when that asset is not the one being borrowed, removed, or liquidated.

### Impact Explanation
If a user's position mask includes an asset whose oracle feed becomes stale or fails to validate (which can happen without any wrongdoing by the caller — feeds naturally go stale between updates, or upstream Pyth/DIA data has issues), that user cannot:
- Withdraw *other*, healthy collateral via `collateral-remove`
- Repay/borrow against *other* assets via `borrow`
- Be liquidated via `liquidate` when they should be, because `get-assets` reverts before health/LTV can even be computed

The liquidation-blocking consequence is the most severe: if a position is undercollateralized and one of the assets in its mask has a broken oracle, liquidators cannot liquidate it at all (the `liquidate` path also calls `get-assets`/`get-notional-evaluation` before reaching liquidation logic), letting bad debt accumulate unchecked while the market continues to accrue further losses. This can lead to insolvency (uncollateralized bad debt piling up because liquidation is blocked) and, at minimum, temporary freezing of funds for the affected users' unrelated healthy assets — both squarely within the in-scope impact classes (protocol insolvency / temporary freezing of funds).

Note that the batch liquidation entry point `liquidate-multi` already applies the report's suggested fix pattern (per-item try/catch instead of an all-or-nothing revert) — see the comment "Failed liquidations return error codes but don't revert entire batch" — [7](#0-6)  confirming the team is aware of and mitigates this exact failure class elsewhere, but `get-assets`'s `unwrap-panic` on `price-multi-resolve` was not given the same treatment.

### Likelihood Explanation
Likelihood is moderate-to-high: oracle staleness is a routine occurrence (any feed can exceed `max-staleness` between updates), and the market supports multiple simultaneous collateral/debt assets per user (up to 64 bits in the mask). Any user who holds a diversified position — which the protocol explicitly encourages via egroups — is exposed the moment any one of their several assets' feeds is stale or errors, without needing an attacker to do anything. No privileged access or DAO compromise is required; this is triggered purely by normal oracle feed timing/availability combined with an ordinary user's multi-asset position.

### Recommendation
Change `get-assets`/`price-multi-resolve` to tolerate per-asset oracle failures gracefully instead of `unwrap-panic`-ing the whole batch:
- For assets not relevant to the specific operation being health-checked (i.e., the operation doesn't need that asset's fresh price to determine safety), fall back to the last cached/known price or exclude that asset's collateral contribution conservatively (treat it as zero collateral value, not remove it from debt) rather than reverting.
- Alternatively, surface a partial-failure response (similar to the `liquidate-multi` list-of-responses pattern) and only require successful price resolution for asset IDs actually being operated on plus asset IDs required for the health check's core LTV math, isolating failures to the specific asset rather than the whole mask.
- At minimum, ensure `liquidate` never depends on a healthy price for assets other than the collateral/debt pair actually being liquidated, so that broken oracles on unrelated assets in a position cannot block liquidation of severely unhealthy debt.

### Proof of Concept
Conceptual PoC (Clarity, illustrating the revert path):
1. User A supplies two collateral assets, e.g. `sBTC` and `USDC`, and borrows against them, giving a position mask with both bits set.
2. `USDC`'s Pyth feed is not refreshed for longer than `max-staleness` (or the DIA identifier for `USDC` returns an error/expired entry) — this requires no attacker action, just normal feed cadence.
3. User A calls `collateral-remove` to withdraw *only* their `sBTC` collateral (unrelated to the stale `USDC` feed).
4. `collateral-remove` calls `get-assets position-mask`, which calls `price-multi-resolve` over **both** `sBTC` and `USDC` oracle idents, then `unwrap-panic`s the result.
5. Because `USDC`'s oracle resolution fails, the `unwrap-panic` panics, the whole `collateral-remove` transaction reverts — User A cannot withdraw their unrelated, healthy `sBTC` collateral.
6. Simultaneously, if User A's debt against this same mask becomes unhealthy, a liquidator calling `liquidate` on User A hits the identical `get-assets mask` call in the `liquidate` flow and also reverts, meaning the position cannot be liquidated while `USDC`'s feed is broken — allowing bad debt to grow unchecked. [1](#0-0) [8](#0-7) [9](#0-8)

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L482-492)
```text
(define-private (get-assets (mask-user uint))
  (let ((mask-enabled (get-enabled-bitmap))
        (safe-mask (user-safe-mask mask-user mask-enabled))
        (iter (mask-to-list-collateral safe-mask))
        (assets-list (get-status-multi iter))
        (oracles-list (map get-oracle assets-list))
        ;; Extract asset-ids for price resolution
        (asset-ids (map get-asset-id assets-list))
        ;; Use internal price resolution
        (prices-list (unwrap-panic (price-multi-resolve oracles-list asset-ids))))
    (map merge-price assets-list prices-list)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1051-1051)
```text
                    (current-assets (get-assets current-mask))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1107-1126)
```text
(define-public (collateral-remove (ft <ft-trait>) (amount uint) (receiver (optional principal)) (price-feeds (optional (list 3 (buff 8192)))))
  (let ((ft-address (contract-of ft))
        (asset (try! (get-asset ft-address)))
        (asset-id (get id asset))
        (account contract-caller)
        (collateral-receiver (match receiver recv recv contract-caller))
        (position (try! (get-position account)))
        (has-debt (> (len (get debt position)) u0)))

    (asserts! (> amount u0) ERR-AMOUNT-ZERO)

    (if has-debt
        ;; HAS DEBT: Full flow with price resolution and health checks
        (let ((is-collateral-enabled (get collateral asset))
              (feeds-check (try! (write-feeds price-feeds)))
              (position-mask (get mask position))
              (pos-full (if is-collateral-enabled position (try! (get-full-position account))))
              (u-debt (accrue-user-debts (get debt pos-full)))
              (u-coll (accrue-user-collateral (get collateral pos-full)))
              (assets (get-assets position-mask))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1258-1258)
```text
        (assets (get-assets mask))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1382-1410)
```text
(define-public (liquidate
                (borrower principal)
                (collateral-ft <ft-trait>)
                (debt-ft <ft-trait>)
                (debt-amount uint)
                (min-collateral-expected uint)
                (collateral-receiver (optional principal))
                (price-feeds (optional (list 3 (buff 8192)))))
  (let (
    (feeds-check (try! (write-feeds price-feeds)))
    (liquidator contract-caller)
    (position (try! (get-liquidation-position borrower)))
    (pos-full (try! (get-full-position borrower)))
    (mask (get mask position))
    (group (try! (get-egroup mask)))

    (coll-address (contract-of collateral-ft))
    (debt-address (contract-of debt-ft))
    (coll-asset (try! (get-asset coll-address)))
    (debt-asset (try! (get-asset debt-address)))
    (coll-aid (get id coll-asset))
    (debt-aid (get id debt-asset))

    ;; accrue FIRST - populates cache for zToken price resolution
    (u-debt (accrue-user-debts (get debt pos-full)))
    (u-coll (accrue-user-collateral (get collateral pos-full)))

    ;; NOW safe to resolve prices (cache is populated)
    (assets (get-assets mask))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1587-1599)
```text
;; Liquidates multiple positions atomically
;; Each position can have different: borrower, collateral asset, debt asset, and debt amount
;; Prevents front-running attacks that prevent bad debt socialization
;; Note: price-feeds not supported in batch - update prices separately or use individual liquidate()
;; Returns list of responses - one per position (ok/err)
;; Failed liquidations return error codes but don't revert entire batch
(define-public (liquidate-multi
                (positions (list 64 { borrower: principal,
                                      collateral-ft: <ft-trait>,
                                      debt-ft: <ft-trait>,
                                      debt-amount: uint,
                                      min-collateral-expected: uint })))
  (ok (map call-liquidate positions)))
```

**File:** docs/oracle.md (L301-317)
```markdown

## Batch Price Fetching

For gas efficiency, multiple prices can be fetched in a single internal call:

```clarity
;; In market.clar
(define-private (price-multi-resolve 
  (data (list 64 {type, ident, callcode}))
  (aids (list 64 uint)))
  (fold iter-price-multi data init))
```

**Use Case:** Market needs prices for multiple assets:
- 5 collateral assets + 3 debt assets = 8 prices
- Single internal resolution instead of 8 separate operations
- Returns list of 8 prices in same order as input
```
