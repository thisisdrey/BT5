### Title
Oracle freshness check treats any future-dated price timestamp as automatically "fresh" with zero deviation limit - ([File: mainnet/contracts/market/v0-4-market.clar])

### Summary
The market contract's oracle freshness validator, `oracle-timestamp-fresh`, is supposed to reject stale price data, but it contains a logic flaw analogous to the reported Blex `FastPriceFeed._setLastUpdatedValues()` issue: any price timestamp that is greater than the current `stacks-block-time` (i.e., in the future) is unconditionally treated as perfectly fresh (`delta = u0`), with no upper bound at all on how far in the future that timestamp may be.

### Finding Description
`oracle-timestamp-fresh` computes the staleness delta as: [1](#0-0) 

```clarity
(define-private (oracle-timestamp-fresh (ts uint) (prev uint) (max-staleness uint))
  (let ((delta (if (> ts stacks-block-time)
                   u0
                   (- stacks-block-time ts))))
    (and
      (<= delta max-staleness)
      (>= ts prev))))
```

When `ts > stacks-block-time` (i.e. the reported price timestamp is in the future relative to the chain), `delta` is forced to `u0`, so `(<= delta max-staleness)` is trivially true regardless of how large `max-staleness` is or how far `ts` actually is ahead of the chain's clock. This is the exact bug class described in the Blex report: the check that is meant to bound how far a price timestamp may deviate into the future never actually bounds it — it is even weaker than the reported `_timestamp < block.timestamp + _maxTimeDeviation` check, which at least imposed a `_maxTimeDeviation` ceiling on future timestamps. Here there is no ceiling whatsoever.

This function gates every price used by the protocol via `price-resolve`: [2](#0-1) 

```clarity
(define-private (price-resolve
  (data { type: (buff 1), ident: (buff 32), callcode: (optional (buff 1)), max-staleness: uint }))
  (let (...)
    (asserts! (and (oracle-price-legal final-price) (oracle-timestamp-fresh timestamp last-update-time max-staleness))
              ERR-ORACLE-INVARIANT)
    (if (> timestamp last-update-time)
        (map-set last-update key timestamp)
        false)
    (ok final-price)))
```

Because `last-update-time` is persisted whenever a new, larger timestamp is accepted, a single future-dated price update (whether from clock skew between the off-chain price publisher/relayer and the Stacks chain, or from a bug in the upstream feed's timestamp generation) permanently raises the floor stored in `last-update`. Every subsequent price for that asset must have `ts >= prev`, and as long as `ts` remains ≥ the previously-recorded (possibly bogus, far-future) value, the "freshness" branch (`delta = u0`) keeps being taken, so the check can never actually detect staleness for that feed going forward — the monotonicity requirement combined with the "future timestamp = always fresh" bug effectively neutralizes the staleness protection for the asset's price feed on a persistent basis.

### Impact Explanation
`price-resolve`/`price-multi-resolve` are the sole source of collateral and debt valuation used throughout `v0-4-market.clar` for borrow limits, health/solvency checks, and liquidation eligibility. If a stuck, incorrect, or attacker-influenced future timestamp is ever accepted for any oracle-backed asset (STX, sBTC, stSTX, USDC, USDH, stSTXbtc/z-tokens), the protocol will keep using that price as "fresh" indefinitely, since the staleness delta is pinned to zero for any `ts` still nominally ahead of `stacks-block-time`. This can cause the protocol to misprice collateral/debt, fail to liquidate undercollateralized positions, or liquidate healthy ones — precisely the "protocol insolvency" outcome flagged in the original report as Critical, since bad debt can accumulate without triggering the intended safety checks.

### Likelihood Explanation
The flaw is a deterministic logic error reachable on every price resolution call, not a rare edge case — it requires no privileged access to trigger, only a future-dated `publish-time`/timestamp reaching `price-resolve`, which can occur from ordinary clock drift between an off-chain price relayer and Stacks block time or a benign bug in the feed's timestamp assignment. Once triggered for a feed, the effect persists because of the monotonic `last-update` floor, making the window of exposure open-ended rather than a single transaction.

### Recommendation
Do not special-case future timestamps to zero delta. Instead, explicitly reject any `ts` greater than `stacks-block-time` (or bound it by a small, fixed maximum forward deviation), mirroring the report's recommendation to replace `_timestamp < block.timestamp + _maxTimeDeviation` with `_timestamp <= block.timestamp`:

```clarity
(define-private (oracle-timestamp-fresh (ts uint) (prev uint) (max-staleness uint))
  (and
    (<= ts stacks-block-time)
    (<= (- stacks-block-time ts) max-staleness)
    (>= ts prev)))
```

### Proof of Concept
1. A price update for asset X is resolved via `price-resolve` with `timestamp` set even slightly greater than the current `stacks-block-time` (achievable through normal off-chain publisher/chain clock skew, or a bug in the feed).
2. In `oracle-timestamp-fresh`, since `ts > stacks-block-time`, `delta` is set to `u0`, so `(<= delta max-staleness)` passes trivially and `(>= ts prev)` also passes since `ts` is new; the price is accepted and `last-update` is updated to this future `ts`. [1](#0-0) 
3. On subsequent calls, as long as the real-world/next reported `ts` is still `>= prev` (the stored future value) and still nominally `> stacks-block-time` (which remains true while the chain's block time catches up), the freshness check continues to pass with `delta = u0` regardless of the asset's actual `max-staleness` configuration, so a genuinely stale price can continue to be used in collateral/debt valuation, borrow, and liquidation decisions.

### Citations

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
