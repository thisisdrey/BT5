### Title
`market::price-resolve` accepts future-dated oracle timestamps and stores them as `last-update`, permanently bricking price resolution for the affected feed once the DIA oracle publishes a single overshot timestamp - (File: mainnet/contracts/market/v0-4-market.clar)

### Summary
`oracle-timestamp-fresh` treats any timestamp greater than `stacks-block-time` as automatically fresh (`delta = u0`) instead of rejecting it, and `price-resolve` unconditionally persists any timestamp that is numerically greater than the previously stored one into the monotonic `last-update` map. A single DIA feed update with an inflated `publish-time` — a value the market has no way to bound from above — gets written into `last-update` and becomes the new floor for the `(>= ts prev)` monotonicity check. Every subsequent, legitimate price update for that feed will have a timestamp smaller than this stored future value and will permanently fail `ERR-ORACLE-INVARIANT`, with no on-chain path to reset `last-update` downward.

### Finding Description
`oracle-timestamp-fresh` is defined as: [1](#0-0) 

```clarity
(define-private (oracle-timestamp-fresh (ts uint) (prev uint) (max-staleness uint))
  (let ((delta (if (> ts stacks-block-time)
                   u0
                   (- stacks-block-time ts))))
    (and
      (<= delta max-staleness)
      (>= ts prev))))
```

Any `ts` greater than the current `stacks-block-time` is forced to `delta = u0`, which trivially satisfies `<= delta max-staleness`. There is no upper bound on how far into the future `ts` may be. This value flows directly out of `resolve-dia`: [2](#0-1) 

```clarity
(define-private (resolve-dia (ident (buff 32)))
  (let ((key (unwrap-panic (from-consensus-buff? (string-ascii 32) ident)))
        (res (try! (call-dia key))))
    ;; DIA returns timestamp in milliseconds, convert to seconds for staleness check
    (ok { value: (get value res), timestamp: (/ (get timestamp res) u1000) })))
```

`price-resolve` then persists this timestamp as the new monotonic floor whenever it is numerically larger than the stored value, with no sanity check against `stacks-block-time`: [3](#0-2) 

```clarity
(define-private (price-resolve
  (data { type: (buff 1), ident: (buff 32), callcode: (optional (buff 1)), max-staleness: uint }))
  (let (...
        (last-update-time (oracle-last-update key))
        (timestamp (get timestamp resolution))
        ...)
    (asserts! (and (oracle-price-legal final-price) (oracle-timestamp-fresh timestamp last-update-time max-staleness))
              ERR-ORACLE-INVARIANT)
    ;; update timestamp if newer
    (if (> timestamp last-update-time)
        (map-set last-update key timestamp)
        false)
    (ok final-price)))
```

`last-update` is a plain map with no decrement/reset function exposed anywhere in the market contract — it is written to only inside `price-resolve`. Once a wildly future timestamp is accepted and stored as `last-update` for a given `{type, ident}` key, every future legitimate DIA publish (whose `publish-time` will be a normal, current timestamp, i.e. `ts < prev`) fails `(>= ts prev)` permanently. Since `oracle-timestamp-fresh` is the sole gate and there is no admin/DAO function to overwrite `last-update` downward, this is an unrecoverable, permanent freeze of that price feed — mirroring the structural shape of the reference bug: a single accepted value forwarded/stored without validation against a sane bound becomes a floor that every subsequent legitimate operation must clear but cannot, with no on-chain remediation.

This is triggerable from an ordinary unprivileged path: any user-facing entry point that supplies `price-feeds` (e.g., `borrow`, `collateral-add`, `liquidate`) routes through `write-feeds`/`price-resolve` for Pyth, but DIA-typed assets resolve directly via `call-dia` → the externally-writable `dia-oracle` contract's `values` map, which is written by `set-value`/`set-multiple-values`. I was unable to confirm from the indexed files whether the on-chain `oracle-updater` for the mainnet DIA contract is a trusted centralized key outside this repo's control (the `dia-oracle.clar` found is a cached third-party requirement, not part of this codebase) — this matters for likelihood since exploitation requires the DIA updater (or a bug/misconfiguration in the DIA pipeline) to publish an overshot `publish-time`, not just an ordinary user action. I flag this as unverified via the tools available; a Devin session with full repo access would be needed to confirm the DIA-updater trust model and whether `set-multiple-values`/timestamp bounds are enforced upstream.

### Impact Explanation
If triggered, every subsequent legitimate price resolution for the affected feed (`{type: TYPE-DIA, ident}`) reverts with `ERR-ORACLE-INVARIANT` inside `price-resolve`. Since `price-resolve`/`price-multi-resolve` back all collateral valuation and health-factor computations for any asset priced through that DIA feed, this permanently freezes `borrow`, `collateral-add`/`collateral-remove` (when a health check triggers price resolution), and `liquidate` for every position that depends on that asset's price — a permanent freezing-of-funds condition for affected users' collateral/debt in that asset, with no possible on-chain recovery (no downward-adjust function for `last-update` exists in the reviewed contract). This lands squarely in the in-scope **High** impact class ("temporary freezing of funds") at minimum, and could reach **Critical** ("permanent freezing of funds") for positions that can no longer be repaid, withdrawn, or liquidated because every code path needing that asset's price reverts.

### Likelihood Explanation
Likelihood depends on whether the DIA `publish-time` value reaching `resolve-dia` can ever exceed `stacks-block-time` by more than the noise/clock-skew that `oracle-timestamp-fresh`'s "future is free" branch was presumably meant to tolerate. This can happen from: (a) a single erroneous/malicious DIA-oracle-updater publish (an operational bug rather than a "third-party stablecoin depeg" or pure "incorrect data" issue, since the root cause — accepting unbounded future timestamps and using them as a permanent floor — is a bug in *this* contract's validation logic, not merely bad third-party data); or (b) unit conversion bugs given DIA timestamps are in milliseconds and divided by `u1000` in `resolve-dia` — an off-by-1000 error upstream would produce a ~1000x inflated `timestamp`, trivially producing a far-future value. Given the explicit design intent visible in the comment ("Not too old (per-asset threshold)") without a corresponding "not too new" bound, this looks like an overlooked edge case rather than a deliberate design choice, making it plausible under normal operational error conditions.

### Recommendation
Reject timestamps that are unreasonably ahead of `stacks-block-time` instead of treating them as automatically fresh, and bound the monotonic update:

```clarity
(define-private (oracle-timestamp-fresh (ts uint) (prev uint) (max-staleness uint))
  (let ((delta (if (> ts stacks-block-time)
                   (- ts stacks-block-time)   ;; do not silently zero-out future skew
                   (- stacks-block-time ts))))
    (and
      (<= delta max-staleness)   ;; symmetric bound: reject too-far-future AND too-stale
      (>= ts prev))))
```

Additionally, cap `map-set last-update` writes so a timestamp can never be persisted more than `max-staleness` (or a small fixed clock-skew tolerance) ahead of `stacks-block-time`, ensuring a single anomalous publish cannot permanently raise the monotonic floor beyond what future legitimate publishes can satisfy. Consider also exposing a DAO-gated recovery function to reset a specific feed's `last-update` entry as a backstop, analogous to how the reference report recommends reconciling against a live floor rather than trusting a stale/erroneous snapshot unconditionally.

### Proof of Concept
Conceptual PoC (values chosen for illustration; exact DIA test harness not present in the indexed files):

1. Asset `USDH` is configured with DIA oracle `{type: TYPE-DIA, ident: "USDH/USD"}` and `max-staleness = 300`.
2. The DIA oracle-updater (or a unit-conversion bug in the DIA pipeline) calls `set-value` with `timestamp` corresponding to, e.g., `stacks-block-time + 10_000_000` (in ms, per DIA's ms-based timestamp).
3. Any user calls `collateral-add`/`borrow` for USDH with `price-feeds` triggering `price-resolve` → `resolve-dia` → returns `timestamp = (stacks-block-time + 10_000_000)` (after ms→s conversion).
4. `oracle-timestamp-fresh` computes `delta = u0` (since `ts > stacks-block-time`), passes the staleness check, and `(>= ts prev)` also passes (first-ever update). `price-resolve` executes `(map-set last-update key timestamp)`, persisting the future value as `last-update`.
5. On the next legitimate DIA publish with a normal, current `publish-time` (`ts2 < last-update`), any subsequent `borrow`/`collateral-add`/`liquidate` touching USDH calls `price-resolve`, which now fails `(>= ts2 prev)` and reverts with `ERR-ORACLE-INVARIANT` — permanently, since no function in `market/v0-4-market.clar` ever decreases `last-update`.

I was not able to locate or execute an actual Clarinet/Vitest test in the indexed files reproducing steps 2–5 end-to-end (the DIA production contract lives outside this repo as a cached requirement); a background Devin session with full repository and test-harness access should write and run this PoC against `local-testing/contracts/market/market.clar` (using `mock-oracle.clar`'s `set-price`, or a DIA-shaped mock allowing a controlled future `timestamp`) to confirm the revert is permanent.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L326-330)
```text
(define-private (resolve-dia (ident (buff 32)))
  (let ((key (unwrap-panic (from-consensus-buff? (string-ascii 32) ident)))
        (res (try! (call-dia key))))
    ;; DIA returns timestamp in milliseconds, convert to seconds for staleness check
    (ok { value: (get value res), timestamp: (/ (get timestamp res) u1000) })))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L365-371)
```text
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
