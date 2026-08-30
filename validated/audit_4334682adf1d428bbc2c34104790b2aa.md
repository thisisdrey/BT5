### Title
Unbounded future oracle timestamps permanently poison price-feed freshness checks and freeze market operations - ([File: mainnet/contracts/market/v0-4-market.clar])

### Summary
`oracle-timestamp-fresh` in `mainnet/contracts/market/v0-4-market.clar` (lines 365-371) never bounds how far an incoming price timestamp may sit in the future relative to `stacks-block-time`. Once such a timestamp is accepted, `price-resolve` (lines 373-395) unconditionally advances `last-update` to it. All subsequent legitimate price updates for that feed require `(>= ts prev)`, so the feed becomes permanently stuck once `prev` is poisoned with a future value, mirroring the reported class of bug: an unvalidated timestamp field that is never checked against a sane upper bound, allowing a single bad/malicious update to permanently break progress for that subsystem (there, the whole chain; here, that price feed and everything depending on it).

### Finding Description
`oracle-timestamp-fresh` at `mainnet/contracts/market/v0-4-market.clar#L365-371`:
```clarity
(define-private (oracle-timestamp-fresh (ts uint) (prev uint) (max-staleness uint))
  (let ((delta (if (> ts stacks-block-time)
                   u0
                   (- stacks-block-time ts))))
    (and
      (<= delta max-staleness)
      (>= ts prev))))
```
When `ts > stacks-block-time` (a timestamp in the future), `delta` is forced to `u0`, making the staleness check `(<= delta max-staleness)` trivially true regardless of how far in the future `ts` is. There is no upper-bound check comparable to the fix recommended in the external report ("less than cometBFT time plus minimum slot time"). This is the exact same bug class: the field that should be validated with both a lower and upper time bound is only checked in one direction.

`price-resolve` at `mainnet/contracts/market/v0-4-market.clar#L373-395` then persists that unvalidated future timestamp as the new baseline:
```clarity
(asserts! (and (oracle-price-legal final-price) (oracle-timestamp-fresh timestamp last-update-time max-staleness))
          ERR-ORACLE-INVARIANT)
(if (> timestamp last-update-time)
    (map-set last-update key timestamp)
    false)
```
Once `last-update` for a `{type, ident}` key is set to a value in the far future, every subsequent legitimate price update (whose real timestamp will be far smaller than the poisoned `prev`) fails the `(>= ts prev)` monotonicity check inside `oracle-timestamp-fresh` and reverts with `ERR-ORACLE-INVARIANT` — permanently, until real chain time catches up to the poisoned value (which could be set arbitrarily far ahead).

Because `price-resolve`/`price-multi-resolve` back every asset price lookup used across the market (health checks, borrow, collateral checks, liquidations, ztoken callcode resolution via `resolve-ztoken`), poisoning a single feed's `last-update` entry freezes every market operation that needs a price for that asset — deposits/withdrawals gated by health checks, borrowing, and liquidations for that asset all become permanently unusable.

### Impact Explanation
This lands on **Critical - permanent freezing of funds**. Once a feed's `last-update` is corrupted with a future timestamp, all collateral/debt operations depending on that asset's price (health checks, borrow, liquidation) revert with `ERR-ORACLE-INVARIANT` indefinitely, locking user collateral and debt positions denominated in or collateralized by that asset with no on-chain recovery path (the map is written by `price-resolve` itself, and no privileged/DAO function was found in scope to reset `last-update`).

### Likelihood Explanation
Triggering this requires only a single price update reporting a timestamp ahead of the current `stacks-block-time` to pass through `resolve-pyth`/`resolve-dia` and reach `price-resolve` — no DAO/registry compromise or privileged access to Zest contracts is needed, only one out-of-range publish timestamp from the upstream price source data path that Zest itself fails to bound. Given oracle publish-time fields are attacker/publisher-influenced data forwarded verbatim into `price-resolve`, and Zest's own validation logic (not the oracle) is what fails to reject a future value, this is a plausible, low-effort trigger once such a value is observed on any feed.

### Recommendation
Bound `oracle-timestamp-fresh` symmetrically: reject `ts` that exceeds `stacks-block-time + max-staleness` (or a small fixed tolerance) instead of silently zeroing `delta` for future timestamps, matching the report's own recommended fix pattern (timestamp must be `> previous` AND `< current time + tolerance`).

### Proof of Concept
1. A Pyth/DIA feed for asset X publishes (or is made to publish) a price with `publish-time = stacks-block-time + N` for some large `N`.
2. Any market operation touching asset X (e.g., a borrow or health check) calls `price-resolve`, which calls `oracle-timestamp-fresh(ts, prev, max-staleness)`; since `ts > stacks-block-time`, `delta = 0 <= max-staleness` passes, and `ts >= prev` passes (prev is normal).
3. `price-resolve` executes `(map-set last-update key ts)`, poisoning the baseline with the future timestamp.
4. On the next legitimate price update, the real `ts' < ts` (poisoned `prev`), so `(>= ts' prev)` fails and `price-resolve` returns `ERR-ORACLE-INVARIANT` for every future call involving asset X, permanently blocking any market function requiring its price. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L76-80)
```text
(define-constant ERR-ORACLE-TYPE (err u400010))
(define-constant ERR-ORACLE-CALLCODE (err u400011))
(define-constant ERR-ORACLE-PYTH (err u400012))
(define-constant ERR-ORACLE-DIA (err u400013))
(define-constant ERR-ORACLE-INVARIANT (err u400014))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L362-371)
```text
(define-private (oracle-price-legal (p uint))
  (> p u0))

(define-private (oracle-timestamp-fresh (ts uint) (prev uint) (max-staleness uint))
  (let ((delta (if (> ts stacks-block-time)
                   u0
                   (- stacks-block-time ts))))
    (and
      (<= delta max-staleness)
      (>= ts prev))))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L373-395)
```text
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
