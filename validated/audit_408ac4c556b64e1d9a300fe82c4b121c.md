## Title
Permissionless callers can bypass mandatory price updates in `liquidate`/`borrow`/`collateral-*` by passing `price-feeds: none`, letting them pick a stale on-chain Pyth price instead of the true current price — (`mainnet/contracts/market/v0-4-market.clar`)

### Summary
`market.clar`'s state-changing entry points (`liquidate`, `borrow`, `collateral-add`, `collateral-remove`) take an *optional* `price-feeds` parameter that is meant to let a caller push a fresh Pyth update atomically before the health/liquidation math runs. Because the parameter is optional and the freshness check only verifies that the cached Pyth price is *not older than* a `max-staleness` window (rather than requiring it to be the actual latest available price), any permissionless caller can simply omit the update (`none`) and exploit whichever price is already resting in Pyth's shared on-chain storage — including a price they themselves pushed earlier — as long as it is still inside the staleness window. This is the same "keeper chooses whether to update the oracle" pattern as Flatmoney H-6, applied to `liquidate`/`borrow` instead of `executeOrder`.

### Finding Description
`liquidate` (and the other hot-path functions) call `write-feeds` first with the caller-supplied `price-feeds`: [1](#0-0) 

`write-feeds` is a no-op when `price-feeds` is `none`: [2](#0-1) 

Price resolution then pulls whatever is currently stored in the shared Pyth storage contract (`pyth-storage-v4.get-price`), which is written by *any* account calling `pyth-oracle-v4.verify-and-update-price-feeds` — completely independent of `market.clar`: [3](#0-2) 

The only freshness guarantee applied by `market.clar` is `oracle-timestamp-fresh`, which merely checks the cached price's timestamp against a per-asset `max-staleness` window and that it isn't older than the previously observed timestamp — it does **not** require the price to be the true latest off-chain price: [4](#0-3) 

Because `pyth-storage-v4`'s `prices` map is a single global cache shared by all callers of the Pyth bridge, and `market.clar` accepts `price-feeds: none` at any hot-path call, a caller can:
1. Push a favorable price snapshot to Pyth storage directly (via `pyth-oracle-v4.verify-and-update-price-feeds`), or simply wait for an existing stale-but-still-"fresh-enough" snapshot to sit in storage (e.g., a transient dip/spike that has since reverted off-chain).
2. Call `market.liquidate(...)` (or `borrow`/`collateral-add`/`collateral-remove`) with `price-feeds: none`, so `market.clar` uses that stale cached price instead of forcing an update to the true current price, as long as `delta <= max-staleness`.

This mirrors the report's root cause precisely: the requirement to use the freshest price is *optional* and bypassable by supplying an empty/`none` update, letting the caller select whichever price (stale-favorable vs. current) benefits them.

### Impact Explanation
For `liquidate`, this directly enables theft of a borrower's collateral: an attacker can trigger liquidation using a stale low collateral valuation (still within `max-staleness`) even though the position is actually healthy at the true current price, seizing collateral plus the liquidation penalty that the victim would not otherwise owe. This is a direct theft of user funds at rest, matching the Critical impact class. Conversely, on `borrow`, a caller could exploit a stale high collateral valuation to over-borrow beyond what the true price would allow, creating protocol insolvency/bad debt exposure once the price catches up — also in scope as protocol insolvency.

### Likelihood Explanation
`liquidate` is explicitly permissionless (`define-public (liquidate ...)` callable by `contract-caller`), and `price-feeds` being `(optional ...)` is a normal, documented calling convention (`none` is used throughout the test suite and in `call-liquidate` for `liquidate-multi`). No additional on-chain mechanism forces the caller to supply a fresh update or verifies that the cached price reflects the true current market state beyond the staleness window — an attacker only needs a transient price excursion (common for BTC/STX) to be reflected in Pyth's shared storage within the `max-staleness` window, which per the project's own docs can be as short as 60 seconds, a realistic window for volatile-asset price dips.

### Recommendation
Require `liquidate` (and other collateral/debt/health-impacting entry points) to receive a mandatory, verifiably fresh price update rather than accepting `none`, or additionally validate that the resolved timestamp is within a much tighter bound of `stacks-block-time` (not just `max-staleness`) for any function that seizes user funds. Alternatively, disallow relying on a globally-shared price cache that any third party can have written; instead, require the caller of `liquidate`/`borrow` to submit price data whose publish-time is provably recent relative to the transaction, and reject stale cache reuse for security-critical health/liquidation calculations.

### Proof of Concept
1. Attacker (or anyone) submits `pyth-oracle-v4.verify-and-update-price-feeds` with a real but transient BTC dip price (e.g., $48,000) at time T1, populating `pyth-storage-v4`'s `prices` map.
2. Off-chain, BTC price recovers to $60,000 by T2 (T2 - T1 < per-asset `max-staleness`, e.g. within 60–120s).
3. Attacker calls `market.liquidate(borrower, sbtc-ft, usdc-ft, debt-amount, min-collateral-expected, none, none)` — passing `price-feeds: none` (see `write-feeds` no-op at `mainnet/contracts/market/v0-4-market.clar:146-152`).
4. `resolve-pyth`/`price-resolve` reads the still-cached $48,000 price (still within `max-staleness`, so `oracle-timestamp-fresh` passes) instead of the true $60,000 price, making `borrower`'s healthy position appear liquidatable.
5. `liquidate` proceeds, seizing borrower collateral and awarding the liquidation penalty to the attacker at the artificially depressed valuation, even though the position is solvent at the real current price.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L146-152)
```text
;; Process optional list of price feed updates
;; If list is provided, folds over it and updates all feeds
;; If list is none, does nothing (allows for backward compatibility)
(define-private (write-feeds (feeds (optional (list 3 (buff 8192)))))
  (match feeds
    entries (fold write-feed entries (ok true))
    (ok true)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L308-320)
```text
(define-private (call-pyth (ident (buff 32)))
  (let ((res (unwrap! (contract-call? 'SP1CGXWEAMG6P6FT04W66NVGJ7PQWMDAC19R7PJ0Y.pyth-storage-v4 get-price ident) ERR-ORACLE-PYTH)))
    (ok res)))

(define-private (resolve-pyth (ident (buff 32)))
  (let ((response (try! (call-pyth ident)))
        (price (get price response))
        (expo (get expo response))
        (conf (get conf response))
        (final-price (normalize-pyth price expo))
        (timestamp (get publish-time response)))
    (try! (check-confidence price conf))
    (ok { value: final-price, timestamp: timestamp })))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L362-395)
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

**File:** mainnet/contracts/market/v0-4-market.clar (L1382-1394)
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
```
