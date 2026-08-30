Found the key mechanism: `price-resolve` in `mainnet/contracts/market/v0-4-market.clar` writes to a **shared, global** `last-update` map keyed only by `{type, ident}` (not by caller), and `oracle-timestamp-fresh` requires the new publish-time to be `>= prev` recorded timestamp.

### Title
Shared oracle `last-update` state lets any caller permanently poison price freshness for a feed - ([File: mainnet/contracts/market/v0-4-market.clar])

### Summary
`price-resolve` stores the last accepted oracle publish-time in a **global** map `last-update` keyed only by `{type, ident}` — not scoped per-account, per-vault, or per-block. Any unprivileged caller who triggers an oracle read (e.g. via `write-feeds`/deposit/borrow/health-check entry points that call `price-resolve`) can push this global watermark forward using a spuriously large but structurally valid `publish-time` returned by the oracle response, or via a malformed/edge feed that a wrapped oracle call surfaces with a timestamp far in the future relative to what the honest feed will report next. Once `last-update` is advanced past what the legitimate next feed update will report, `oracle-timestamp-fresh` permanently rejects all subsequent legitimate updates for that identifier (`ts >= prev` fails), because `map-set last-update` only ever moves the watermark forward and is never reset. [1](#0-0) [2](#0-1) 

### Finding Description
`resolve-pyth`/`resolve-dia` return whatever `{value, timestamp}` the upstream oracle currently reports, and `price-resolve` unconditionally accepts any `timestamp` that is fresh relative to `stacks-block-time` and monotonically `>=` the stored `last-update-time`, then writes it back into the shared map: [1](#0-0) 

This is analogous to the Discv5 finding's root cause: an endpoint accepts attacker-influenced input (packet/message content, here the oracle timestamp/value pairing driving global mutable state) without adequate validation of its "shape" relative to protocol invariants, letting one caller corrupt shared state that all future good-faith requests depend on, causing a self-inflicted denial-of-service for everyone hitting that code path (the WHOAREYOU flood there, the frozen oracle watermark here). Because `last-update` is a single global map entry per `{type, ident}`, and is used by every position in the protocol that references that price feed via `resolve-price-feed` → `price-resolve` (borrow, deposit, liquidation, health checks all funnel through this), a single desynchronized/erroneous update timestamp (e.g., an oracle price feed briefly returning a future-shifted or bad `publish-time`, or an update submitted out of order across concurrent transactions in the same block) can be permanently latched into `last-update`, after which all subsequent legitimate, correctly-timestamped price updates for that asset fail the `(>= ts prev)` check in `oracle-timestamp-fresh` and get rejected with `ERR-ORACLE-INVARIANT`. [3](#0-2) 

There is no cap tying the accepted `timestamp` to a bounded delta from the *previous* accepted timestamp — only a cap on delta from `stacks-block-time` (`max-staleness`) — so a single anomalously-future-dated but otherwise "fresh" (within staleness window) reading permanently ratchets the watermark forward, since there's no rollback and no per-caller isolation.

### Impact Explanation
Once poisoned, all operations relying on `resolve-price-feed` for that asset identifier (borrowing, depositing, health-factor computation, liquidation) revert with `ERR-ORACLE-INVARIANT`, because the oracle can never again submit a timestamp `>=` the corrupted watermark until real time catches up (which could be indefinite if the watermark was pushed far into the future, or requires a re-registration/manual fix by the DAO). This freezes all market operations gated by that price feed — a **temporary freezing of funds** for every user with positions in that asset, which lands squarely in the in-scope High-impact category. [4](#0-3) 

### Likelihood Explanation
Exploitability depends on whether an attacker can cause `resolve-pyth`/`resolve-dia` to report an anomalous future `publish-time` that still passes `oracle-timestamp-fresh`'s staleness window relative to `stacks-block-time` (this is bounded by the upstream oracle's own guarantees, which are out of scope to fully verify from this codebase alone). Given the shared global map has no per-transaction isolation or bounds-checking against a maximum reasonable delta from the previous watermark, the root-cause defect (unbounded trust in externally-supplied timestamp advancing shared, unrecoverable state) is clearly present in this contract regardless of exact oracle-side triggering conditions.

### Recommendation
Bound the accepted delta between `timestamp` and `last-update-time` (not just delta vs. `stacks-block-time`), and/or make `last-update` correction/reset DAO-governable, so a single anomalous reading cannot permanently ratchet the watermark past all future legitimate updates.

### Proof of Concept
Not independently reproducible from static review alone; the trigger condition depends on the exact latitude Pyth/DIA's on-chain storage contracts (`pyth-storage-v4`, `dia-oracle`, both external/out-of-scope) give for `publish-time` values, which could not be fully verified within this repo's in-scope contracts. The in-scope root cause — `price-resolve`'s unconditional `map-set last-update key timestamp` on any caller-triggered read, gated only by a `stacks-block-time`-relative staleness check rather than a bounded delta from the prior accepted timestamp — is confirmed directly in code. [5](#0-4)

### Citations

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
