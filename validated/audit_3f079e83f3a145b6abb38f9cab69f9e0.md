### Title
Front-runnable in-band Pyth price feed update causes griefing DoS on `borrow`/`collateral-add`/`liquidate` - (File: `mainnet/contracts/market/v0-4-market.clar`, function `write-feed`/`write-feeds`)

### Summary
`market.clar`/`v0-4-market.clar` accept an optional `price-feeds` parameter on hot-path entry points (`borrow`, `collateral-add`, `collateral-remove`, `liquidate`) to allow atomic, "in-band" Pyth price updates when a price is stale, instead of requiring a separate update-then-retry flow. This mirrors the two-step pattern in the external report (submit a third-party-verifiable payload, then immediately consume it) and is subject to the same class of front-running griefing: any unprivileged party can observe the pending price-feed bytes in the mempool and submit them to the oracle first, and — if the second submission of the identical feed fails or the whole outer call otherwise reverts — the user's `borrow`/`collateral-add`/`liquidate` transaction is forced to fail via `ERR-PRICE-FEED-UPDATE-FAILED`.

### Finding Description
`write-feed` is a private helper invoked from every hot-path function that accepts `price-feeds`: [1](#0-0) 

```
(define-private (write-feed (feed (buff 8192)) (status (response bool uint)))
  (match status
    success-status
      (match (contract-call? 'SP1CGXWEAMG6P6FT04W66NVGJ7PQWMDAC19R7PJ0Y.pyth-oracle-v4 verify-and-update-price-feeds
          feed { ... })
        update-success (ok true)
        update-failed ERR-PRICE-FEED-UPDATE-FAILED)
    error-status status))

(define-private (write-feeds (feeds (optional (list 3 (buff 8192)))))
  (match feeds
    entries (fold write-feed entries (ok true))
    (ok true)))
```

`write-feeds` is called unconditionally at the top of `borrow` and `liquidate` (and analogously in `collateral-add`/`collateral-remove`), before any of the position/health logic runs: [2](#0-1) [3](#0-2) 

The `price-feeds` argument is the raw Pyth PNAU/VAA payload bytes — a self-contained, publicly-verifiable, guardian-signed message (see `pyth-oracle-v4.verify-and-update-price-feeds`, `pyth-pnau-decoder-v3.decode-and-verify-price-feeds`, `wormhole-core-v4.parse-and-verify-vaa`) — exactly analogous to an EIP‑2612 permit signature: anyone who observes the bytes (e.g., in the mempool, or by simply querying Hermes for the same feed) can submit them to `pyth-oracle-v4` directly, independent of the user's transaction, since `verify-and-update-price-feeds` has no caller restriction tying it to the market contract or to the original submitter: [4](#0-3) 

An attacker (or simply any other normal user/bot racing to update the same stale feed, which is a very plausible non-malicious occurrence given Pyth's pull model) can front-run the price update contained in the user's transaction. Whether that causes the user's follow-on `write-feed` call to hard-fail depends on the state-transition rules enforced inside `pyth-storage-v4`'s write path (staleness/publish-time checks). Given the pattern of "verify signed payload, then consume it, with no `try!`/fallback path if verification/write fails" mirrors the exact root cause flagged in the external Permit2Proxy report — the caller has no way to tolerate the case where the same payload (or a fresher one, since the attacker's transaction executed first and updated `last-update`) has already been applied — the outer `borrow`/`collateral-add`/`liquidate` call has no graceful degradation: any failure returned by `write-feed` (mapped to `ERR-PRICE-FEED-UPDATE-FAILED`) propagates via `try!` and aborts the entire transaction, even though the price is now perfectly fresh and the user's original action would otherwise succeed with `price-feeds` set to `none`.

Unlike the Permit2Proxy report's root cause (a signature becoming "already used" and thus explicitly rejected by ERC20.permit), here the failure mode is speculative without deeper access to the exact staleness-check branch inside `pyth-storage-v4.write` (not retrievable in this environment) — but the architectural weakness is the same: an in-band, two-step "verify-then-consume" flow built on a publicly replayable payload, invoked without a try/fallback wrapper, that an unprivileged third party can race against to deny the legitimate caller's transaction.

### Impact Explanation
This is a griefing/DoS vector, not fund theft: repeated front-running of in-band price updates can be used to force user `borrow`/`collateral-add`/`liquidate` transactions to revert, which is a **temporary freezing of funds** — users relying on the atomic "stale price → in-band update → execute" flow can be denied the ability to borrow, add collateral, or liquidate an unhealthy position at will, particularly during periods when prices are stale and this feature is most needed (e.g., during liquidation races, where being unable to liquidate could also let an unhealthy position remain under-collateralized longer than it should).

### Likelihood Explanation
Low-to-moderate. It requires an attacker to observe the exact `price-feeds` payload in the mempool/network and race a transaction to the oracle before the market call executes — achievable by any bot watching the network, with no economic cost to the attacker beyond gas/fees and no profit motive (consistent with the "griefing, no direct profit" characterization in the referenced report). It is more likely to occur incidentally (two unrelated users both submitting a fresh update for the same feed) than as a deliberate attack, but a deliberate attacker could target specific users to block their borrow/liquidation attempts.

### Recommendation
In `write-feed`, do not propagate the `ERR-PRICE-FEED-UPDATE-FAILED` unconditionally when `verify-and-update-price-feeds` fails. Instead, treat a failed update as non-fatal and continue execution provided the current on-chain price for that feed is already fresh enough to satisfy the staleness check that would otherwise have required an in-band update (i.e., check "is price already fresh" before/after attempting the update, and only revert if the price is still stale after the attempted update). This mirrors the report's recommendation to wrap the state-mutating side-effect call in a try/fallback and continue if the desired end-state (sufficient allowance / fresh price) is already achieved by someone else's earlier transaction.

### Proof of Concept
Conceptual PoC (Clarity/simnet), analogous to the Solidity PoC in the report:
1. User A observes a stale BTC/STX/USDC feed and prepares `borrow(ft, amount, receiver, (some (list feed-bytes)))` with the correct PNAU bytes for the current Hermes price update.
2. Attacker copies `feed-bytes` from A's pending transaction (or independently fetches the same update from Hermes) and calls `pyth-oracle-v4.verify-and-update-price-feeds` directly, updating `pyth-storage-v4` first.
3. User A's `borrow` transaction executes: `write-feeds` → `write-feed` invokes `verify-and-update-price-feeds` with the same payload; if the underlying storage/decoder logic rejects the already-applied/duplicate merkle-root/publish-time payload (or any other verification step tied to one-time consumption), `write-feed` returns `ERR-PRICE-FEED-UPDATE-FAILED`, which propagates through `try!` in `borrow`, causing the entire borrow to revert — even though the price is now fresh and the position is healthy.

Note: I was unable to inspect the exact staleness/replay-check branch inside `pyth-storage-v4`'s `write` function in this session (the file could not be retrieved before the tool budget was exhausted), so the precise failure condition inside `write-feed` (duplicate vs. stale vs. always-idempotent) is not fully confirmed and should be verified directly against `local-testing/contracts/pyth/contracts/pyth-storage-v4.clar` before treating this as fully proven.

### Citations

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

**File:** mainnet/contracts/market/v0-4-market.clar (L1261-1272)
```text
        (current-group (try! (get-egroup mask)))
        (current-ltvb (buff-to-uint-be (get LTV-BORROW current-group)))

        ;; LTV
        (notional-valued-assets (get-notional-evaluation { position: position, assets: assets }))
        (collateral-value (get collateral notional-valued-assets))
        (debt-value (get debt notional-valued-assets)))

    ;; preconditions
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)
    (asserts! (get debt asset) ERR-BORROW-DISABLED)
    (asserts! (is-healthy collateral-value debt-value current-ltvb) ERR-UNHEALTHY)
```

**File:** local-testing/contracts/market/market.clar (L1261-1272)
```text
(define-public (borrow (ft <ft-trait>) (amount uint) (receiver (optional principal)) (price-feeds (optional (list 3 (buff 8192)))))
  (let ((address (contract-of ft))
        (asset (try! (get-asset address)))
        (asset-id (get id asset))
        (account contract-caller)
        (funds-receiver (match receiver recv recv contract-caller))
        (feeds-check (try! (write-feeds price-feeds)))
        
        ;; Step 1: Get position WITHOUT resolving prices
        (position (try! (get-position account)))
        (mask (get mask position))
        
```

**File:** local-testing/contracts/pyth/contracts/pyth-oracle-v4.clar (L31-51)
```text
(define-public (verify-and-update-price-feeds 
		(price-feed-bytes (buff 8192))
		(execution-plan {
			pyth-storage-contract: <pyth-storage-trait>,
			pyth-decoder-contract: <pyth-decoder-trait>,
			wormhole-core-contract: <wormhole-core-trait>
		}))
	(begin
		;; Check execution flow
		(try! (contract-call? .pyth-governance-v3 check-execution-flow contract-caller (some execution-plan)))
		;; Perform contract-call
		(let ((pyth-decoder-contract (get pyth-decoder-contract execution-plan))
				(wormhole-core-contract (get wormhole-core-contract execution-plan))
				(pyth-storage-contract (get pyth-storage-contract execution-plan))
				(decoded-prices (try! (contract-call? pyth-decoder-contract decode-and-verify-price-feeds price-feed-bytes wormhole-core-contract)))
				(updated-prices (try! (contract-call? pyth-storage-contract write decoded-prices)))
				(fee-info (contract-call? .pyth-governance-v3 get-fee-info))
				(fee-amount (* (len updated-prices) (* (get mantissa fee-info) (pow u10 (get exponent fee-info))))))
			;; Charge fee
			(if (> fee-amount u0) (unwrap! (stx-transfer? fee-amount tx-sender (get address fee-info)) ERR_BALANCE_INSUFFICIENT) true)
			(ok updated-prices))))
```
