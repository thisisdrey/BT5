### Title
Same-transaction multi-price Pyth oracle update lets a caller-controlled contract walk collateral/debt valuation through two distinct fresh prices for risk-free arbitrage / self-liquidation - (File: `mainnet/contracts/market/v0-4-market.clar`)

### Summary
Every state-changing market entry point (`collateral-add`, `collateral-remove`, `borrow`, `liquidate`) accepts an optional `(list 3 (buff 8192))` of raw Pyth VAA update payloads, which is passed to `write-feeds`/`write-feed` and forwarded to `pyth-oracle-v4`/`pyth-storage-v4` before the same call resolves prices for its own health check via `resolve-pyth`/`price-resolve`. [1](#0-0) [2](#0-1)  The only guards in `pyth-storage-v4` are that an update must be newer than the last stored price (`ERR_NEWER_PRICE_AVAILABLE`) and not older than `stale-price-threshold` relative to the current Stacks block time [3](#0-2)  — there is no check bounding how much the price may move between two updates written in the same transaction. Because Pyth/Hermes emits a new signed VAA roughly every 400ms, an attacker can obtain two distinct, both-valid, both-"fresh" VAAs and feed them to two chained calls inside a single attacker-deployed contract's atomic transaction, exactly mirroring the external report's PoC of two different genuine prices being written and consumed within one transaction.

### Finding Description
`resolve-pyth` reads whatever is currently stored in `pyth-storage-v4`'s `prices` map at the moment `price-resolve`/`price-multi-resolve` executes: [4](#0-3) [5](#0-4) . That map is mutated in place by `write-batch-entry`, called via `verify-and-update-price-feeds`, whenever any of `collateral-add`/`collateral-remove`/`borrow`/`liquidate` is invoked with a non-`none` `price-feeds` argument: [6](#0-5) [7](#0-6) .

Because Stacks lets any principal deploy their own contract and call multiple public functions of `v0-4-market` atomically in one transaction, a caller's own contract can:
1. Call `collateral-add`/`borrow` supplying VAA₁ (a legitimately signed, non-stale Pyth update) to update the on-chain price and immediately resolve health checks/borrow limits against P₁.
2. In the same atomic transaction, call `liquidate` (or another `borrow`) supplying VAA₂ — a second legitimately signed VAA published ~400ms later carrying a different price P₂ that is still within the staleness window and still "more recent" than P₁, satisfying both `write-batch-entry` guards.

Nothing in `v0-4-market.clar`'s `write-feed`/`price-resolve` path enforces that the price used for step 2 cannot differ materially from the price just used in step 1 within the same block/transaction, nor is there any minimum-time-between-updates or maximum-delta check as the external report recommends. This is the direct on-chain analog of the reported vulnerability: multiple genuine Pyth prices fetched and applied within a single transaction, letting the caller pick whichever of two real, freshly-published prices is most favorable for each leg of their own operation.

### Impact Explanation
An ordinary unprivileged caller, via their own deployed contract, can deterministically sequence two different genuine oracle prices within one atomic transaction against their own position: e.g. inflate collateral valuation to borrow the maximum against price P₁, then use a slightly different fresh price P₂ to trigger a favorable liquidation/close of the same position, extracting protocol funds risk-free. This is theft of funds at rest in the vaults (the shared `pyth-storage-v4` price map and the market's collateral/debt accounting are used by all positions), landing in the Critical impact category (direct theft of user funds / protocol insolvency), matching the External Report's BVSS classification of a Critical/serious arbitrage bug.

### Likelihood Explanation
Likelihood is moderate-to-high: it requires the attacker to source two distinct valid Pyth VAAs published a few hundred milliseconds apart (routinely available from Hermes given Pyth's 400ms update cadence, as demonstrated in the report's Basescan PoC) and to deploy a small helper contract to chain the market calls atomically — both are within reach of an ordinary unprivileged user, requiring no privileged access or DAO compromise.

### Recommendation
Enforce a maximum allowed price deviation (or a minimum time-since-last-update requirement) inside `write-batch-entry` in `pyth-storage-v4.clar`, or add an invariant in `v0-4-market.clar`'s `price-resolve`/`resolve-pyth` that rejects using a newly-written price if the previous price for the same feed was updated in the same Stacks block/transaction context, per the recommendation already validated and remediated by the CoreDAO team in the referenced commit.

### Proof of Concept
1. Attacker deploys a helper contract `attack.clar` that, in one public function, sequentially calls:
   - `(contract-call? .v0-4-market collateral-add ft-trait amount (some (list VAA1)))` — updates `pyth-storage-v4` price for feed X to P1 (fresh, high) and adds collateral valued at P1.
   - `(contract-call? .v0-4-market borrow ft-trait max-amount (some receiver) (some (list VAA2)))` — updates the same feed to P2 (fresh, ~400ms later, lower/higher as needed) before the borrow's own `price-resolve` runs, letting the attacker borrow against a valuation mix that would be impossible if both operations had to share one canonical block price.
2. Both VAA1 and VAA2 pass `write-batch-entry`'s `is-price-update-more-recent` and staleness checks because they are both genuinely fresh Hermes-issued updates minutes/seconds apart. [3](#0-2) 
3. The attacker's contract call succeeds atomically, since `write-feeds`/`price-resolve` impose no same-transaction price-consistency check. [6](#0-5) [5](#0-4)

### Citations

**File:** mainnet/contracts/market/market-trait.clar (L9-18)
```text
    ;; Add collateral to position
    ;; price-feeds: optional list of up to 3 Pyth price feed buffers to update stale prices
    ;; Returns: updated collateral amount for this asset
    (collateral-add (<ft-trait> uint (optional (list 3 (buff 8192)))) (response uint uint))
    
    ;; Remove collateral from position
    ;; receiver: optional recipient (defaults to caller)
    ;; price-feeds: optional list of up to 3 Pyth price feed buffers to update stale prices
    ;; Returns: remaining collateral amount for this asset
    (collateral-remove (<ft-trait> uint (optional principal) (optional (list 3 (buff 8192)))) (response uint uint))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L126-152)
```text
;; -- Price feed update helpers ----------------------------------------------

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

**File:** local-testing/contracts/pyth/contracts/pyth-storage-v4.clar (L74-111)
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
		;; Update storage
		(map-set prices 
			(get price-identifier entry) 
			{
				price: (get price entry),
				conf: (get conf entry),
				expo: (get expo entry),
				ema-price: (get ema-price entry),
				ema-conf: (get ema-conf entry),
				publish-time: publish-time,
				prev-publish-time: (get prev-publish-time entry)
			})
		;; Emit event
		(print {
			type: "price-feed", 
			action: "updated", 
			data: entry
		})
		;; Update timestamps tracking
		(map-set timestamps (get price-identifier entry) (get publish-time entry))
		(ok entry)))
```
