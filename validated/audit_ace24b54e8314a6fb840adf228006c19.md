## Finding [1](#0-0) 

### Title
Permissionless price-feed updates can be raced to make the shared `last-update` timestamp overtake a legitimate borrower/repayer/liquidator's already-signed price, causing their transaction to revert - ([File: mainnet/contracts/market/v0-4-market.clar])

### Summary
The `op::execute`-style DoS in the report (a permissionless, repeatable state-mutating call that blocks a privileged/critical downstream operation via a strict ordering check) has an analog in the market contract's oracle price resolution. `price-resolve` stores the highest-seen price timestamp per oracle feed in a **global**, permissionless-writable map `last-update`, and enforces `(>= ts prev)` on every subsequent price submission for that feed. Since any user (borrower, repayer, liquidator, withdrawer) can push a fresh Pyth price update through `write-feeds` inside their own market call, an attacker can race to always be first to "consume" the newest available Pyth VAA, bumping the shared `prev` value. Any other legitimate, already-signed-and-broadcast transaction carrying a slightly older (but still valid/non-stale) price for the same feed will then fail the monotonicity check and revert.

### Finding Description
`price-resolve` reads and writes the shared, per-feed `last-update` map and gates on strict monotonicity: [2](#0-1) 

```
(define-private (oracle-timestamp-fresh (ts uint) (prev uint) (max-staleness uint))
  ...
    (>= ts prev)))

(define-private (price-resolve ...)
  ...
    (asserts! (and (oracle-price-legal final-price) (oracle-timestamp-fresh timestamp last-update-time max-staleness))
              ERR-ORACLE-INVARIANT)
    (if (> timestamp last-update-time)
        (map-set last-update key timestamp)
        false)
    (ok final-price)))
```

`price-resolve` is reached from any market entry point that accepts an optional `price-feeds` argument (e.g. `borrow`, `repay`, `collateral-add`, `collateral-remove`, `liquidate`) via `write-feeds`, which is itself unauthenticated - callable by any `contract-caller`: [3](#0-2) 

Because `last-update` is keyed only by `{ type, ident }` (the oracle feed identity) and not by user/position, it is shared global state. Any account can submit a market call (even a trivial/self-liquidating one, or a zero-impact operation with `price-feeds` set) as soon as a newer Pyth VAA becomes available, which advances `last-update` for that feed to the newest timestamp. A pending transaction from another user that was signed/broadcast slightly earlier, embedding an equally valid (non-stale) but now-older timestamp for the same feed, will have `ts < prev` at execution time and revert with `ERR-ORACLE-INVARIANT`, wasting the user's gas and stalling their borrow/repay/liquidate/withdraw.

This mirrors the report's root cause exactly: a permissionless, arbitrarily-repeatable state mutation (`start_balance_sync`-equivalent = "push a fresher price") that a privileged/critical downstream operation (`op::execute`-equivalent = borrow/repay/liquidate/collateral-remove) checks against via a strict inequality, and which anyone can trigger to make that check fail for other users.

### Impact Explanation
An attacker (or several colluding/automated bots watching Hermes for new Pyth publish times) can perpetually front-run the `last-update` map for any actively-traded feed used by the market, causing other users' borrow, repay, liquidate, or collateral-remove/redeem transactions to revert with `ERR-ORACLE-INVARIANT` whenever those transactions carry a price embed that becomes stale-relative-to-`prev` by the time they are mined. This is a temporary but indefinitely-repeatable denial of service against the market's core debt/collateral operations for any user, which falls under "temporary freezing of funds" (High impact) since users cannot reliably repay debt, avoid liquidation, or withdraw collateral, and the interference is sustainable as long as the attacker keeps submitting minor, low-cost market transactions with the freshest oracle data.

### Likelihood Explanation
Likelihood is Low/Medium: it requires the attacker to actively monitor Pyth publish times and race other users' transactions, and it costs gas plus the Pyth update fee per griefing transaction, and it only causes reverts (not fund loss) for victims whose transactions overlap with the race window. Still, no privileged role or special access is required — any ordinary account can execute this purely through normal market entry points.

### Recommendation
Do not gate `oracle-timestamp-fresh` on a shared, globally-advanced `prev` that any unrelated transaction can bump. Instead, validate only that the submitted price is within `max-staleness` of `stacks-block-time` (which is already checked), and drop or relax the `(>= ts prev)` global monotonicity requirement, or scope `last-update` per-transaction/per-position context rather than as protocol-wide state that arbitrary market calls can advance ahead of a legitimate user's in-flight transaction.

### Proof of Concept
1. Alice queries Hermes for the latest BTC/USD Pyth update (timestamp T1) and broadcasts `borrow` with `price-feeds` containing that VAA.
2. Before Alice's transaction is mined, an attacker (Bob) fetches a newer Pyth update (timestamp T2 > T1) — trivially available seconds later from the public Hermes API — and submits any market call (e.g., a self `collateral-add` with a 0-impact amount) embedding it, which via `write-feeds` → `price-resolve` sets `last-update[BTC] = T2`.
3. Alice's transaction is then mined; `price-resolve` evaluates `oracle-timestamp-fresh(T1, T2, max-staleness)`, and since `T1 < T2`, `(>= ts prev)` is false, causing `ERR-ORACLE-INVARIANT` and reverting Alice's `borrow`.
4. Bob can repeat this every time a new Pyth price is published (frequently, often sub-block), indefinitely blocking Alice's ability to complete borrow/repay/liquidate/withdraw operations that depend on that feed.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L117-120)
```text
;; -- Oracle timestamp tracking
(define-map last-update
  { type: (buff 1), ident: (buff 32) }
  uint)
```

**File:** mainnet/contracts/market/v0-4-market.clar (L128-152)
```text
;; Write a single Pyth price feed update using fold accumulator pattern
(define-private (write-feed (feed (buff 8192)) (status (response bool uint)))
  (match status
    success-status
      (match (contract-call? 'SP1CGXWEAMG6P6FT04W66NVGJ7PQWMDAC19R7PJ0Y.pyth-oracle-v4 verify-and-update-price-feeds
          feed
          {
            pyth-storage-contract: 'SP1CGXWEAMG6P6FT04W66NVGJ7PQWMDAC19R7PJ0Y.pyth-storage-v4,
            pyth-decoder-contract: 'SP1CGXWEAMG6P6FT04W66NVGJ7PQWMDAC19R7PJ0Y.pyth-pnau-decoder-v3,
            wormhole-core-contract: 'SP1CGXWEAMG6P6FT04W66NVGJ7PQWMDAC19R7PJ0Y.wormhole-core-v4,
          }
        )
        update-success (ok true)
        update-failed ERR-PRICE-FEED-UPDATE-FAILED)
    error-status status
  )
)

;; Process optional list of price feed updates
;; If list is provided, folds over it and updates all feeds
;; If list is none, does nothing (allows for backward compatibility)
(define-private (write-feeds (feeds (optional (list 3 (buff 8192)))))
  (match feeds
    entries (fold write-feed entries (ok true))
    (ok true)))
```

**File:** local-testing/contracts/market/market.clar (L384-417)
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
