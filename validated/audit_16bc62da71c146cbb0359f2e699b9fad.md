## Analysis

`process-debt-asset` calls `(unwrap-panic (find-asset debt-aid assets))` — a hard **runtime panic**, not a recoverable `(err ...)` — where `debt-aid` is derived from an attacker/liquidator-supplied `debt-ft` trait reference and `assets` is the list of the *borrower's egroup-enabled* assets [1](#0-0) . Later in `liquidate`, `(unwrap-panic (get-cached-indexes debt-aid))` is used again to compute `remaining-debt-to-repay`, relying on the cache having been populated earlier purely by `accrue-user-debts`/`accrue-user-collateral` over the *borrower's own* position lists [2](#0-1) . `process-collateral-asset` similarly hard-panics via `(unwrap-panic (price-resolve oracle-data))` when the given `coll-aid` is not in the enabled `assets` list [3](#0-2) .

`liquidate-multi` is explicitly documented as non-atomic: *"Liquidates multiple positions atomically... Failed liquidations return error codes but don't revert entire batch"*, and is implemented as `(ok (map call-liquidate positions))` [4](#0-3) . `call-liquidate` simply forwards liquidator-supplied `collateral-ft`/`debt-ft`/`borrower` into `liquidate` [5](#0-4) .

In Clarity, `unwrap-panic` on a `none`/`err` is a hard runtime abort of the entire transaction — the direct functional analog of Solidity's uncaught `revert` inside `ECDSA.recover`. Because `map` (unlike `fold` with an accumulator that can "skip" failures) has no way to catch an aborted call, a single malformed/mismatched entry inside the `positions` list passed to `liquidate-multi` (e.g., a `debt-ft`/`collateral-ft` whose asset id is not present in the target borrower's egroup-enabled `assets` list, or that was never in the borrower's debt/collateral list so the index cache was never populated) causes `find-asset`/`get-cached-indexes`/`price-resolve` to hit their `unwrap-panic` and abort the *entire* `liquidate-multi` call — not just the one failing position. This directly breaks the code's own explicit non-atomic guarantee, exactly matching the M-3 pattern where an unhandled revert deep in per-item validation defeats an intentionally non-atomic batch API.

### Title
`liquidate-multi`'s documented non-atomic guarantee is broken by `unwrap-panic` in `liquidate`'s per-item asset/index lookups - (File: `mainnet/contracts/market/v0-4-market.clar`)

### Summary
`liquidate-multi` is documented and implemented to process a batch of liquidations non-atomically, returning per-position `(response ... uint)` results so that one failing liquidation does not block the others [4](#0-3) . However, the underlying `liquidate` function uses `unwrap-panic` on asset/index lookups (`find-asset`, `get-cached-indexes`, `price-resolve`) that are keyed off caller-supplied `collateral-ft`/`debt-ft` and the specific borrower's accrued position state [1](#0-0) [2](#0-1) . When one of these lookups fails for a single entry in the batch, Clarity aborts the whole transaction rather than returning an isolated error, contradicting the explicit non-atomic contract of `liquidate-multi`/`call-liquidate`.

### Finding Description
`liquidate-multi` maps `call-liquidate` over up to 64 attacker-supplied position specs, each independently invoking the public `liquidate` function [4](#0-3) [5](#0-4) . Inside `liquidate`, several helper calls rely on `unwrap-panic` instead of `try!`/`unwrap!`, meaning a `none`/`err` result there is a fatal runtime abort rather than a caught `(err ...)`:
- `process-debt-asset` does `(unwrap-panic (find-asset debt-aid assets))`, where `assets` is only the borrower's egroup-enabled assets, and `debt-aid` is resolved from the liquidator-supplied `debt-ft` [1](#0-0) .
- `process-collateral-asset` does `(unwrap-panic (price-resolve oracle-data))` for disabled collateral not found in `assets` [3](#0-2) .
- `liquidate` itself does `(unwrap-panic (get-cached-indexes debt-aid))` when computing `remaining-debt-to-repay`, relying on the index cache having been populated for `debt-aid` by `accrue-user-debts`/`accrue-user-collateral`, both of which only iterate the borrower's *own* debt/collateral entries — not the arbitrary `debt-aid` chosen by the liquidator [2](#0-1) .

Any unprivileged liquidator can include, in a `liquidate-multi` batch, one position whose `borrower`/`collateral-ft`/`debt-ft` combination doesn't match the borrower's real position (e.g., specifying a `debt-ft` for an asset the borrower has never borrowed, or after the debt is already at zero from an earlier entry in the same batch acting on the same borrower/asset). This triggers an `unwrap-panic` abort deep in `liquidate`, which propagates up through `call-liquidate` and `map`, aborting the entire `liquidate-multi` transaction — silently discarding all the other, valid liquidations that would otherwise have succeeded in that same call.

### Impact Explanation
This maps to the **temporary freezing of funds** impact class: a legitimate batch of liquidations (which exist specifically to allow liquidators to race and prevent bad-debt buildup — the code comment even states the batch API's purpose is "Prevents front-running attacks that prevent bad debt socialization" [6](#0-5) ) can be forced to fail entirely because of one malformed/stale entry, even though the function's contract promises isolated failures. This can delay timely liquidation and bad-debt socialization of unhealthy positions, and can be weaponized by a third party (e.g., an unrelated liquidator submitting a conflicting/duplicate position in the same mempool window, or simple race conditions between two liquidators targeting overlapping positions) to grief a liquidator's batch transaction, causing legitimate liquidations (and associated debt socialization) to be delayed.

### Likelihood Explanation
Likelihood is moderate: it requires an attacker (or just unlucky non-malicious concurrency) to get one invalid/stale position into someone else's `liquidate-multi` batch, or for a liquidator to naively batch positions without perfectly fresh state (e.g., two entries targeting the same borrower/asset where the first liquidation already zeroes out the debt cache entry relevant to the second). Because `liquidate-multi` explicitly advertises batch reliability as its selling point over sequential individual `liquidate()` calls, users are likely to rely on it under exactly the conditions (races, stale batches) where this panic is most likely to be triggered.

### Recommendation
Replace the `unwrap-panic` calls in `process-debt-asset`, `process-collateral-asset`, and the `remaining-debt-to-repay` calculation in `liquidate` with graceful `unwrap!`/`try!` patterns that return a proper `(err ...)` response instead of aborting. Since `liquidate` is `(response {...} uint)`, this already fits the existing signature; the batch wrapper `call-liquidate`/`liquidate-multi` will then correctly surface these as individual `(err ...)` entries in the returned list without aborting the other positions, restoring the documented non-atomic behavior.

### Proof of Concept
1. Liquidator submits `liquidate-multi` with two entries: entry A liquidates `(borrower, collateral-ft, debt-ft)` fully repaying the tracked debt for `debt-aid`; entry B (same batch) also targets `borrower`/`debt-aid` (e.g., a duplicate or a second liquidator's race condition scenario), or entry B specifies a `debt-ft`/`collateral-ft` for an asset not present in the borrower's enabled `assets` list (e.g., asset was disabled between construction and execution of the batch, or borrower never held debt for that asset).
2. During execution of entry B inside the same `map call-liquidate` call, `process-debt-asset`'s `(unwrap-panic (find-asset debt-aid assets))` (or the `remaining-debt-to-repay` `unwrap-panic (get-cached-indexes debt-aid)`) hits a `none`, causing a Clarity runtime abort.
3. The abort propagates through `call-liquidate` and `map`, causing the entire `liquidate-multi` transaction — including entry A's otherwise-valid liquidation — to revert, contrary to the function's documented guarantee that "Failed liquidations return error codes but don't revert entire batch."

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L761-784)
```text
(define-private (process-debt-asset
  (debt-amount uint)
  (debt-aid uint)
  (max-debt-usd uint)
  (assets (list 64 {
    id: uint, addr: principal, decimals: uint,
    oracle: { type: (buff 1), ident: (buff 32), callcode: (optional (buff 1)), max-staleness: uint },
    collateral: bool, debt: bool, price: uint
  })))
  (let ((debt-asset-info (unwrap-panic (find-asset debt-aid assets)))
        (debt-price (get price debt-asset-info))
        (debt-decimals (get decimals debt-asset-info))
        (debt-usd (normalize (* debt-amount debt-price) debt-decimals false))
        ;; cap debt at maximum liquidatable amount
        (debt-actual-usd (if (> debt-usd max-debt-usd) max-debt-usd debt-usd))
        ;; convert capped USD amount back to token amount
        (debt-actual (mul-div-down debt-actual-usd (pow u10 debt-decimals) debt-price)))
    {
      debt-actual-usd: debt-actual-usd,
      debt-actual: debt-actual,
      debt-price: debt-price,
      debt-decimals: debt-decimals
    }))

```

**File:** mainnet/contracts/market/v0-4-market.clar (L809-815)
```text
        (coll-asset-info (match (find-asset coll-aid assets)
                           ;; Found in enabled list: use it (already has price)
                           found found
                           ;; Not found (disabled): resolve price on demand
                           (let ((oracle-data (get oracle coll-asset))
                                 (price (unwrap-panic (price-resolve oracle-data))))
                             (merge coll-asset { price: price }))))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L907-918)
```text
(define-private (call-liquidate (position { borrower: principal,
                                            collateral-ft: <ft-trait>,
                                            debt-ft: <ft-trait>,
                                            debt-amount: uint,
                                            min-collateral-expected: uint }))
  (liquidate (get borrower position)
             (get collateral-ft position)
             (get debt-ft position)
             (get debt-amount position)
             (get min-collateral-expected position)
             none   ;; collateral-receiver defaults to liquidator
             none)) ;; price-feeds not supported in batch - update prices separately
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1477-1486)
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
    (coll-final (if (is-eq remaining-debt-to-repay u0) user-coll-balance coll-final-raw)))
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
