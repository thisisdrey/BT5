Based on my investigation, the strongest analog to the missing `openDeadline` enforcement is in the oracle freshness/staleness check used across `market.clar`, which is explicitly in-scope ("oracle resolution and callcode transforms").

### Title
Oracle freshness check silently treats future-dated price timestamps as zero-age, bypassing `max-staleness` deadline enforcement - ([File: mainnet/contracts/market/v0-4-market.clar])

### Summary
`oracle-timestamp-fresh` is meant to enforce that a price feed's timestamp is within a bounded age (`max-staleness`) of the current block time — effectively a "deadline" on how old accepted price data may be, analogous to `openDeadline` bounding how long a gasless order may remain openable. When the reported oracle timestamp is greater than `stacks-block-time`, the function forces `delta` to `u0` instead of computing/enforcing an actual bound, so the staleness deadline check is not enforced at all for that branch.

### Finding Description
The staleness/deadline check is defined as: [1](#0-0) 

```clarity
(define-private (oracle-timestamp-fresh (ts uint) (prev uint) (max-staleness uint))
  (let ((delta (if (> ts stacks-block-time)
                   u0
                   (- stacks-block-time ts))))
    (and
      (<= delta max-staleness)
      (>= ts prev))))
```

This is invoked from `price-resolve`, the central function that all collateral/debt valuation, health checks, and liquidation math depend on: [2](#0-1) 

The intended purpose of `max-staleness` is to bound how old a price may be before it is rejected — this is the protocol's analog to `openDeadline`: a time bound on validity that must be enforced against `stacks-block-time`. However, whenever `ts > stacks-block-time` (i.e., the oracle-reported timestamp is in the future relative to the chain's current block time), the code sets `delta` to `u0` unconditionally, which trivially satisfies `(<= delta max-staleness)` regardless of how far in the future `ts` is, and also trivially satisfies `(>= ts prev)`. This mirrors the `openFor()` bug exactly: a time-bound field exists in the data (the timestamp/`max-staleness` deadline concept) but the enforcing comparison is not actually applied in this branch — the check is bypassed instead of failing closed.

### Impact Explanation
Because `price-resolve` feeds directly into `get-asset-value`/`get-notional-evaluation`, which determine collateral value, debt value, and health-factor computations used by `borrow`, `collateral-add`, `collateral-remove`, and `liquidate`, an unenforced staleness bound on the future-timestamp branch means the protocol's health/liquidation math can accept price data whose timestamp validity is not actually bounded as designed. Depending on how the underlying oracle sources (`resolve-pyth`/`resolve-dia`) can be caused to report a timestamp ahead of `stacks-block-time` (e.g. clock skew, feed injection paths, or replay via `write-feeds`/`verify-and-update-price-feeds`), this could let stale/incorrect price data pass the freshness gate that is supposed to reject it, leading to mispriced collateral/debt and unsafe borrow/liquidation decisions — a form of temporary freezing or theft of funds through unsafe leverage if exploitable end-to-end.

### Likelihood Explanation
Medium-low: this requires an oracle-reported timestamp to be ahead of `stacks-block-time`, which is not something an ordinary principal directly controls through market.clar's public entrypoints, but is a defensive check that should fail closed rather than silently pass. The bug is in this code's freshness-check logic (in scope), independent of whether the upstream oracle data itself is trustworthy.

### Recommendation
Do not zero out `delta` when `ts > stacks-block-time`; instead, either reject the price outright (treat a future timestamp as invalid/unhealthy input) or compute the absolute difference and still enforce it against `max-staleness`, e.g.:

```clarity
(define-private (oracle-timestamp-fresh (ts uint) (prev uint) (max-staleness uint))
  (and
    (<= ts stacks-block-time)
    (<= (- stacks-block-time ts) max-staleness)
    (>= ts prev)))
```

This ensures the staleness "deadline" is always enforced rather than bypassed for one branch.

### Proof of Concept
1. An oracle price update is written (via `write-feeds` → `verify-and-update-price-feeds` or the DIA path) with a `publish-time`/timestamp `ts` greater than the current `stacks-block-time` (future-dated).
2. A user calls `borrow`, `collateral-add`, `collateral-remove`, or `liquidate` with `price-feeds` supplied, triggering `price-resolve` → `oracle-timestamp-fresh(ts, prev, max-staleness)`.
3. Since `ts > stacks-block-time`, `delta` is forced to `u0`, so `(<= delta max-staleness)` passes unconditionally, and `(>= ts prev)` also passes trivially since a future `ts` is greater than any previously recorded timestamp.
4. The price is accepted as "fresh" regardless of the actual configured `max-staleness` bound, and is used in subsequent health/liquidation calculations — the intended time-bound enforcement never actually executes for this data. [3](#0-2)

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L365-395)
```text
(define-private (oracle-timestamp-fresh (ts uint) (prev uint) (max-staleness uint))
  (let ((delta (if (> ts stacks-block-time)
                   u0
                   (- stacks-block-time ts))))
    (and
      (<= delta max-staleness)
      (>= ts prev))))

(define-private (price-resolve
  (data { type: (buff 1), ident: (buff 32), callcode: (optional (buff 1)), max-staleness: uint }))
  (let ((type (get type data))
        (ident (get ident data))
        (key { type: type, ident: ident })
        (resolution (try! (resolve-price-feed type ident)))
        (price (get value resolution))
        (callcode (get callcode data))
        (final-price (try! (resolve-callcode price callcode)))
        (last-update-time (oracle-last-update key))
        (timestamp (get timestamp resolution))
        (max-staleness (get max-staleness data)))

    ;; validate price and timestamp using max-staleness from oracle data
    (asserts! (and (oracle-price-legal final-price) (oracle-timestamp-fresh timestamp last-update-time max-staleness))
              ERR-ORACLE-INVARIANT)

    ;; update timestamp if newer
    (if (> timestamp last-update-time)
        (map-set last-update key timestamp)
        false)

    (ok final-price)))
```
