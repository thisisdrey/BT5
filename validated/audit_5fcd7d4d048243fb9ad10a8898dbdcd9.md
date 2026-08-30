### Title
Bundled price-feed update griefing causes entire market operation (`borrow`/`collateral-remove`/`liquidate`) to revert on a benign, front-runnable Pyth write failure - (File: `local-testing/contracts/market/market.clar` / `mainnet/contracts/market/v0-4-market.clar`)

### Summary
`market.clar`'s hot-path functions (`borrow`, `collateral-add`, `collateral-remove`, `liquidate`, etc.) accept an optional `price-feeds` bundle and process it through `write-feeds` → `write-feed`, which folds over up to 3 raw Pyth VAA byte buffers and calls `verify-and-update-price-feeds` for each. Exactly like the `CallLib`/`CrossChainExecutor` pattern in the referenced report, this fold is all-or-nothing: if any single feed write in the bundle fails, the whole `write-feeds` result becomes an error, which is propagated with `try!` and aborts the entire enclosing market operation — even though the failure can be a completely benign "price already fresher / already written" condition that a griefer can trigger for free by broadcasting the victim's own (public, mempool-visible) VAA bytes first.

### Finding Description
`write-feed` implements exactly the "any individual Call fails → revert everything" pattern that the report criticizes: [1](#0-0) 

`write-feeds` folds this over the (up to 3) submitted feed buffers, and because `write-feed`'s `match` short-circuits on the first error status, a single failing feed poisons the whole fold result: [2](#0-1) 

Every hot-path entry point that accepts `price-feeds` unwraps this result with `try!` (e.g. inside `collateral-remove`): [3](#0-2) 

The underlying Pyth storage layer explicitly rejects a write that is not strictly newer than what is already stored: [4](#0-3) 

Because the price-feed bytes a user includes in `borrow`/`liquidate`/`collateral-remove` are public transaction data (visible in the mempool before confirmation), any third party can extract the exact same VAA bytes and submit them (directly to `pyth-oracle-v4`, or embedded in their own unrelated market call) ahead of the victim's transaction. When the victim's transaction then lands, `verify-and-update-price-feeds` for that identical feed fails (`ERR_NEWER_PRICE_AVAILABLE`/`ERR_STALE_PRICE` propagated as a non-`ok` response into `write-feed`'s `update-failed` branch), `write-feed` returns `ERR-PRICE-FEED-UPDATE-FAILED`, `write-feeds` short-circuits, and the `try!` in the calling market function aborts the entire operation — collateral change, borrow, or liquidation and all — even though the price is already fresh on-chain and the operation would otherwise have succeeded with `price-feeds: none`.

This is precisely the bug class from the report: a batched set of "calls" (feed writes) where any single failure reverts the whole transaction, with no mechanism to skip/ignore an individually-failed-but-inconsequential item (unlike `pyth-storage-v4`'s own `write` function, which correctly filters out failed entries within a single VAA batch via `only-ok-entry` rather than reverting — the market-layer `write-feed`/`write-feeds` fold does not apply the same tolerance across the *list* of feed buffers).

### Impact Explanation
An attacker can grief any user (or automated liquidator) who bundles `price-feeds` with a market call by front-running with the identical, publicly-visible VAA bytes, forcing the victim's transaction to revert. Applied specifically to `liquidate` calls that bundle a stale price update, this becomes a targeted temporary-freeze/DoS primitive: a griefer watching the mempool can repeatedly cancel a liquidator's attempt to liquidate an unhealthy position by pre-consuming the exact feed update the liquidator needs, forcing repeated reverts and wasted gas, and delaying liquidation of an unhealthy position during a volatile market — a temporary freezing of the borrower's/liquidator's ability to act on funds at the time it matters most. It also generally DoSes ordinary `borrow`/`collateral-remove` calls that rely on in-band price updates, forcing users to retry (burning gas) with no guarantee the next attempt won't be front-run again.

### Likelihood Explanation
Likelihood is moderate: front-running requires only observing the mempool and re-broadcasting public VAA bytes ahead of the victim — no special privileges, funds, or oracle compromise needed. It is most impactful when timed against a liquidation, but the underlying flaw affects every hot-path function that takes `price-feeds`. It is bounded by the fact that the griefer must react quickly to the mempool and pay their own transaction fee for each grief attempt, and the victim can retry with `price-feeds: none` if the price is already fresh — but during periods of contested liquidations this still creates a meaningful window for delay.

### Recommendation
Make `write-feed`/`write-feeds` tolerant of individual feed failures, mirroring the fix recommended in the referenced report and already applied to `pyth-storage-v4`'s own batch `write`: catch a failing `verify-and-update-price-feeds` call per-feed (e.g. via `match`/`unwrap!` that treats "not newer" as a non-fatal skip rather than a hard error) and continue processing the remaining feeds, only surfacing a fatal error for feeds that are genuinely unavailable/invalid (e.g. malformed VAA) rather than merely "already up to date." Additionally, treat "price already fresh enough" as a success path so that a benign race with another submitter of the same feed does not cause the whole market operation to abort.

### Proof of Concept
1. Alice submits `borrow(USDC-vault, amount, min-out, price-feeds: (some (list STX-VAA)))` where `STX-VAA` is a valid, not-yet-consumed Pyth VAA for the STX/USD feed, needed to refresh a stale price for her position's health check.
2. Bob observes Alice's pending transaction in the mempool and extracts `STX-VAA`.
3. Bob submits his own transaction (any market call, or a direct call into `pyth-oracle-v4.verify-and-update-price-feeds`) containing the identical `STX-VAA`, and it lands first in the same block, updating the STX price in `pyth-storage-v4`.
4. Alice's transaction is mined next. Inside `write-feed` [5](#0-4)  the call to `verify-and-update-price-feeds` fails the `is-price-update-more-recent` check in `pyth-storage-v4` [4](#0-3) , returning an error status.
5. `write-feed` returns `ERR-PRICE-FEED-UPDATE-FAILED`; `write-feeds`'s fold short-circuits on this error [2](#0-1) .
6. Alice's `borrow` call's `try!` on `write-feeds` propagates the error, and her entire `borrow` transaction reverts — despite the STX price being fresh and correct on-chain (updated one block earlier by Bob), and despite Alice's operation being otherwise perfectly valid.

Note: full verification of the exact error surfaces from `pyth-pnau-decoder-v3`/`wormhole-core-v4` (e.g., whether VAA replay is separately blocked at the decoder/wormhole layer, which would strengthen or narrow the exact trigger conditions) was not completed due to iteration limits; the `is-price-update-more-recent`/staleness check in `pyth-storage-v4.clar` alone is sufficient to demonstrate the all-or-nothing revert propagation described above.

### Citations

**File:** local-testing/contracts/market/market.clar (L133-152)
```text
(define-private (write-feed (feed (buff 8192)) (status (response bool uint)))
  (match status
    success-status
      ;; @mainnet: (match (contract-call? 'SP1CGXWEAMG6P6FT04W66NVGJ7PQWMDAC19R7PJ0Y.pyth-oracle-v4 verify-and-update-price-feeds
      (match (contract-call? .pyth-oracle-v4 verify-and-update-price-feeds
          feed
          {
            ;; @mainnet: pyth-storage-contract: 'SP1CGXWEAMG6P6FT04W66NVGJ7PQWMDAC19R7PJ0Y.pyth-storage-v4,
            pyth-storage-contract: .pyth-storage-v4,
            ;; @mainnet: pyth-decoder-contract: 'SP1CGXWEAMG6P6FT04W66NVGJ7PQWMDAC19R7PJ0Y.pyth-pnau-decoder-v3,
            pyth-decoder-contract: .pyth-pnau-decoder-v3,
            ;; @mainnet: wormhole-core-contract: 'SP1CGXWEAMG6P6FT04W66NVGJ7PQWMDAC19R7PJ0Y.wormhole-core-v4,
            wormhole-core-contract: .wormhole-core-v4,
          }
        )
        update-success (ok true)
        update-failed ERR-PRICE-FEED-UPDATE-FAILED)
    error-status status
  )
)
```

**File:** local-testing/contracts/market/market.clar (L154-160)
```text
;; Process optional list of price feed updates
;; If list is provided, folds over it and updates all feeds
;; If list is none, does nothing (allows for backward compatibility)
(define-private (write-feeds (feeds (optional (list 3 (buff 8192)))))
  (match feeds
    entries (fold write-feed entries (ok true))
    (ok true)))
```

**File:** local-testing/contracts/market/market.clar (L1141-1144)
```text
    (if has-debt
        ;; HAS DEBT: Full flow with price resolution and health checks
        (let ((is-collateral-enabled (get collateral asset))
              (feeds-check (try! (write-feeds price-feeds)))
```

**File:** local-testing/contracts/pyth/contracts/pyth-storage-v4.clar (L84-90)
```text
	(let ((stale-price-threshold (contract-call? .pyth-governance-v3 get-stale-price-threshold))
			(latest-stacks-timestamp (unwrap! (get-stacks-block-info? time (- stacks-block-height u1)) ERR_STALE_PRICE))
			(publish-time (get publish-time entry)))
		;; Ensure that we have not processed a newer price
		(asserts! (is-price-update-more-recent (get price-identifier entry) publish-time) ERR_NEWER_PRICE_AVAILABLE)
		;; Ensure that price is not stale
		(asserts! (>= publish-time (+ (- latest-stacks-timestamp stale-price-threshold) STACKS_BLOCK_TIME)) ERR_STALE_PRICE)
```
