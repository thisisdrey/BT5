### Title
Oracle freshness check clamps future timestamps to zero, allowing a legitimately-signed price update to permanently poison the monotonic timestamp guard and freeze price resolution - (File: mainnet/contracts/market/v0-4-market.clar)

### Summary
`v0-4-market.clar`'s `oracle-timestamp-fresh` is the sole freshness guard used during price resolution (`price-resolve`). Analogous to the IBC report's flaw — where a composite timeout condition is only partially validated (checking `revision_height` while ignoring `revision_number`, letting the check be trivially satisfied) — this function only partially validates the "freshness" condition: when the reported publish `ts` is *greater* than the current `stacks-block-time`, the code silently clamps `delta` to `u0` instead of rejecting or otherwise bounding the mismatch, unconditionally satisfying the staleness bound. [1](#0-0) 

### Finding Description
`price-resolve` fetches a price/timestamp pair from `resolve-price-feed` (Pyth or DIA) and validates it purely with:

```clarity
(define-private (oracle-timestamp-fresh (ts uint) (prev uint) (max-staleness uint))
  (let ((delta (if (> ts stacks-block-time)
                   u0
                   (- stacks-block-time ts))))
    (and
      (<= delta max-staleness)
      (>= ts prev))))
``` [1](#0-0) 

The upstream `call-pyth` path uses `pyth-storage-v4.get-price`, which returns the raw stored entry without re-checking staleness (the storage contract's own bound is only enforced at *write* time, and even then only as a lower bound — `write-batch-entry` never rejects an upper-bound/"too far in the future" `publish-time`) [2](#0-1) . This means Zest's own `oracle-timestamp-fresh` is the only defense against a publish timestamp that runs ahead of the chain's `stacks-block-time` (which can legitimately drift due to normal cross-system clock skew between Pyth/DIA network time and Stacks block time — no malicious oracle data is required, just a `ts` that happens to be even slightly ahead of `stacks-block-time`).

When `ts > stacks-block-time`, `delta` is forced to `u0`, so `(<= delta max-staleness)` is *always* true regardless of how large the actual gap is. The price is then accepted and, critically, `price-resolve` persists it as the new watermark:

```clarity
(if (> timestamp last-update-time)
    (map-set last-update key timestamp)
    false)
``` [3](#0-2) 

Because `last-update` is now set to a timestamp in the future relative to `stacks-block-time`, every subsequent legitimate price update for that feed must satisfy `(>= ts prev)` against this inflated watermark. Real-time, honestly-published prices will fail this monotonicity check until the chain's block time catches up to the poisoned value, at which point `ERR-ORACLE-INVARIANT` is raised for every operation that needs that asset's price.

Price feeds are supplied by ordinary callers through the public `price-feeds` parameter accepted by market entry points (`borrow`, `liquidate`, etc.), which route through `write-feeds`/`write-feed` into `pyth-oracle-v4.verify-and-update-price-feeds` before `price-resolve` runs [4](#0-3) . Any user can therefore trigger price resolution using a validly-signed VAA whose `publish-time` happens to be ahead of the current `stacks-block-time` and cause this poisoning as a side effect of a normal transaction — no compromise of Pyth/DIA infrastructure is needed.

### Impact Explanation
Once `last-update` for an asset's oracle key is poisoned into the future, every function relying on `price-resolve`/`get-asset-value` for that asset — `borrow`, `repay`, `collateral-add`/`collateral-remove` (health-check paths), and `liquidate` — reverts with `ERR-ORACLE-INVARIANT` until real time passes the poisoned watermark. This is a protocol-wide denial of service on that asset's market operations: users cannot withdraw collateral, cannot be liquidated (protecting unhealthy positions from timely liquidation and risking protocol insolvency if prices move against frozen positions), and cannot repay debt normally. This matches the in-scope "temporary freezing of funds" (and, if it also blocks timely liquidation of a market-wide asset during a price crash, risks insolvency), landing on the **High** impact class (temporary freezing of funds), with potential escalation toward Critical if it prevents liquidations long enough to create bad debt.

### Likelihood Explanation
The trigger condition — a validly-signed oracle `publish-time` slightly ahead of `stacks-block-time` — does not require any oracle compromise, only normal clock/network skew between the Pyth/DIA publishing infrastructure and the Stacks chain's block timestamp, or a user choosing to submit a VAA whose timestamp is marginally ahead. Given `price-feeds` is a caller-supplied parameter on common entry points, any ordinary user can trigger the vulnerable code path. The severity of the resulting freeze (duration) depends on how far ahead the poisoning timestamp is, so likelihood of a *meaningful* (multi-block) freeze is moderate, but the underlying logic bug is deterministic and always reachable.

### Recommendation
Remove the `delta = 0` clamp for future timestamps. If `ts > stacks-block-time`, the update should be rejected outright (or bounded by an explicit maximum allowed clock-skew tolerance) rather than being treated as maximally fresh. Additionally, consider bounding the amount by which `last-update`/watermark can advance beyond `stacks-block-time`, so a single future-dated publish cannot indefinitely block subsequent legitimate updates.

### Proof of Concept
1. Wait for (or construct/select) a validly Pyth-guardian-signed VAA for asset X whose `publish-time` is even 1 second ahead of the Stacks chain's current `stacks-block-time` (achievable due to ordinary clock/propagation skew between Pyth's network and Stacks blocks, or by delaying submission of a recent VAA until block time is momentarily behind).
2. Call any market entry point that accepts `price-feeds` (e.g., `borrow`) supplying this VAA for asset X.
3. `write-feeds` → `pyth-oracle-v4.verify-and-update-price-feeds` writes the update into `pyth-storage-v4` (no upper-bound staleness check exists there either).
4. `price-resolve` calls `resolve-pyth`, obtains `timestamp = publish-time > stacks-block-time`; `oracle-timestamp-fresh` computes `delta = u0`, passes; `last-update` map is set to this future `timestamp`.
5. Any subsequent transaction (by any user) needing asset X's price — `borrow`, `repay`, `collateral-remove`, `liquidate` — calls `price-resolve` again; the newly-fetched real-time price has `timestamp < last-update` (the poisoned future watermark), so `(>= ts prev)` fails, and `ERR-ORACLE-INVARIANT` is raised, blocking the operation until `stacks-block-time` naturally advances past the poisoned watermark. [3](#0-2)

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

**File:** local-testing/contracts/pyth/contracts/pyth-storage-v4.clar (L74-90)
```text
(define-private (write-batch-entry (entry {
		price-identifier: (buff 32),
		price: int,
		conf: uint,
		expo: int,
		ema-price: int,
		ema-conf: uint,
		publish-time: uint,
		prev-publish-time: uint,
	}))
	(let ((stale-price-threshold (contract-call? .pyth-governance-v3 get-stale-price-threshold))
			(latest-stacks-timestamp (unwrap! (get-stacks-block-info? time (- stacks-block-height u1)) ERR_STALE_PRICE))
			(publish-time (get publish-time entry)))
		;; Ensure that we have not processed a newer price
		(asserts! (is-price-update-more-recent (get price-identifier entry) publish-time) ERR_NEWER_PRICE_AVAILABLE)
		;; Ensure that price is not stale
		(asserts! (>= publish-time (+ (- latest-stacks-timestamp stale-price-threshold) STACKS_BLOCK_TIME)) ERR_STALE_PRICE)
```

**File:** local-testing/contracts/market/market.clar (L1411-1414)
```text
                (collateral-receiver (optional principal))
                (price-feeds (optional (list 3 (buff 8192)))))
  (let (
    (feeds-check (try! (write-feeds price-feeds)))
```
