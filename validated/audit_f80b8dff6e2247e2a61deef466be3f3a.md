### Title
Unprivileged liquidator can panic-abort `liquidate-multi` batch via an uncached asset index lookup, griefing all bundled liquidations - ([File: mainnet/contracts/market/v0-4-market.clar])

### Summary
`liquidate-multi` is a public entry point that lets any caller bundle up to 64 independent liquidations into a single atomic transaction, explicitly documented as "Failed liquidations return error codes but don't revert entire batch" [1](#0-0) . That guarantee is broken because the underlying `liquidate` function contains several `unwrap-panic (get-cached-indexes ...)` calls whose safety depends on the target asset id having already been accrued into `index-cache` earlier in the same call. That cache population only happens for the assets actually present in the borrower's own collateral/debt lists [2](#0-1) , but the `debt-aid`/`coll-aid` used later are derived directly from the caller-supplied `debt-ft`/`collateral-ft` trait arguments [3](#0-2) , with no check that these correspond to assets actually held by `borrower`. If a caller supplies (or a position's debt/collateral composition legitimately does not include) the given `debt-ft`, `get-cached-indexes debt-aid` returns `none` and the subsequent `unwrap-panic` triggers a hard runtime panic rather than a graceful error, at `scale-debt-for-liquidation` [4](#0-3)  and again at the "remaining debt" computation inside `liquidate` [5](#0-4) .

### Finding Description
This is the same bug class as the referenced Celestia fix: a validation/consistency check that should only cause a hard failure for a single, isolated unit of work instead causes a hard panic that propagates and destroys unrelated work in the same execution context — exactly analogous to "only the bridge node should panic," where a full/light node handling many peers' data should not crash the whole process over one bad input.

In `liquidate-multi`, each entry is dispatched via `call-liquidate`/`liquidate` and collected with `map`, not `try!`, which is correct for *returned* `(response ...)` errors — those are captured per-entry without aborting the batch [1](#0-0) . However, Clarity's `unwrap-panic` does not produce a `(response ... err)` value that `map` can capture — it raises a runtime abort that terminates the entire enclosing transaction, unwinding every state change made by every other (valid, healthy) liquidation already processed in the same `liquidate-multi` call.

The `unwrap-panic (get-cached-indexes asset-id)` pattern is used repeatedly in `liquidate`/`scale-debt-for-liquidation` under the assumption that `accrue-user-debts`/`accrue-user-collateral` (run once, over the borrower's actual position lists) have already warmed the cache for every asset id that will later be looked up [2](#0-1) . That assumption silently breaks whenever `debt-aid`/`coll-aid` (chosen from the caller-supplied `<ft-trait>` arguments) do not appear in the borrower's own debt/collateral lists — a state entirely reachable by an ordinary, unprivileged caller simply by choosing an asset pair not actually owed/held by the targeted borrower, or by racing a legitimate liquidator's batch with a state-changing transaction (e.g., repay/withdraw the specific asset) between batch construction and execution so that one entry's assumption becomes stale by execution time.

### Impact Explanation
Because a single non-panicking `(response ... err)` would have been isolated by `map`, but the actual failure mode is a raw runtime panic, one malformed or raced entry in a `liquidate-multi` call reverts the *entire* transaction — undoing all other bundled liquidations that were otherwise valid and healthy for the batch. This directly contradicts the contract's own stated invariant that this function exists specifically "to prevent front-running attacks that prevent bad debt socialization" [6](#0-5) . An attacker (or even a bystander taking an unrelated legitimate action, e.g. a normal repay) can use this to grief/delay the liquidation of severely undercollateralized positions bundled in the same batch, increasing the window during which bad debt accrues and must later be socialized against all lenders — a temporary freezing of funds/delayed loss mitigation that can escalate toward protocol insolvency if repeated during volatile price moves.

### Likelihood Explanation
Likelihood is moderate-to-high: `liquidate-multi` is a public, unprivileged entry point, and liquidators routinely batch multiple borrowers/assets to save gas. Any mismatch between the caller-supplied `debt-ft`/`collateral-ft` and the borrower's actual holdings at execution time — whether from a stale liquidation bot, a race with the borrower's own repay/withdraw, or a deliberately crafted entry inserted by a third party submitting a competing transaction — is sufficient to trigger the panic and grief the whole batch.

### Recommendation
Replace the `unwrap-panic (get-cached-indexes asset-id)` calls in `scale-debt-for-liquidation` and inline in `liquidate` with an explicit `unwrap!`/`asserts!` returning a proper error code, and validate upfront (before any cache-dependent computation) that `debt-aid`/`coll-aid` are actually present in the borrower's debt/collateral lists, returning a graceful error (e.g., `ERR-NO-POSITION`) instead of allowing the transaction to panic. This preserves the documented per-entry error isolation of `liquidate-multi`.

### Proof of Concept
1. Borrower B holds collateral in asset A and debt in asset X only (no position in asset Y).
2. Liquidator batches `liquidate-multi` with two entries: entry 1 legitimately liquidates an unrelated, healthy-to-liquidate borrower C; entry 2 targets borrower B but supplies `debt-ft` = asset Y (which B does not owe).
3. During execution of entry 2, `accrue-user-debts`/`accrue-user-collateral` only cache indexes for B's actual assets (A, X) [2](#0-1) ; asset Y is never cached.
4. `scale-debt-for-liquidation` (or the remaining-debt branch) calls `unwrap-panic (get-cached-indexes Y)` which returns `none` and panics [7](#0-6) .
5. The panic aborts the entire `liquidate-multi` transaction, reverting entry 1's otherwise-successful liquidation of borrower C along with entry 2, despite the contract's documented guarantee that failed entries "don't revert entire batch."

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L858-868)
```text
(define-private (scale-debt-for-liquidation
  (debt-final uint)
  (coll-actual uint)
  (curr-scaled uint)
  (asset-id uint))
  (let (;; convert debt amount to scaled units for storage
        (borrow-index (get index (unwrap-panic (get-cached-indexes asset-id))))
        (scaled-debt (mul-div-down debt-final INDEX-PRECISION borrow-index))
        ;; cap at current debt (prevent over-repayment)
        (scaled-to-remove (if (> scaled-debt curr-scaled) curr-scaled scaled-debt))
        (debt-to-repay (mul-div-up scaled-to-remove borrow-index INDEX-PRECISION))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1398-1403)
```text
    (coll-address (contract-of collateral-ft))
    (debt-address (contract-of debt-ft))
    (coll-asset (try! (get-asset coll-address)))
    (debt-asset (try! (get-asset debt-address)))
    (coll-aid (get id coll-asset))
    (debt-aid (get id debt-asset))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1405-1407)
```text
    ;; accrue FIRST - populates cache for zToken price resolution
    (u-debt (accrue-user-debts (get debt pos-full)))
    (u-coll (accrue-user-collateral (get collateral pos-full)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1477-1485)
```text
    (remaining-debt-to-repay
      (if (> coll-remaining u0)
        (let ((rem-coll-usd (normalize (* coll-remaining coll-price) coll-decimals false))
              (rem-debt-usd (div-bps-down rem-coll-usd (+ BPS liq-penalty-max)))
              (rem-debt-tokens (mul-div-down rem-debt-usd (pow u10 debt-decimals) debt-price))
              (rem-borrow-index (get index (unwrap-panic (get-cached-indexes debt-aid))))
              (rem-scaled (mul-div-down rem-debt-tokens INDEX-PRECISION rem-borrow-index)))
          (mul-div-up rem-scaled rem-borrow-index INDEX-PRECISION))
        u1))
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
