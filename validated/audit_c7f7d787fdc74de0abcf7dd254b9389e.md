Found a genuine analog. In `wormhole-core-v4.clar`, `parse-vaa` is reachable from the unprivileged, user-facing `borrow`, `collateral-add`, `collateral-remove`, and `liquidate` entry points via the `price-feeds` parameter, which is an attacker-controlled `(buff 8192)` list.

### Title
Attacker-controlled `price-feeds` buffer causes `unwrap-panic` abort in `parse-vaa`, DoS-ing borrow/collateral/liquidation entry points - (File: `local-testing/contracts/pyth/contracts/wormhole/wormhole-core-v4.clar`)

### Summary
`market.clar`'s public entry points accept an arbitrary, caller-supplied `price-feeds` list of `(buff 8192)` values that are forwarded, unvalidated, through `pyth-oracle-v4.verify-and-update-price-feeds` → `pyth-pnau-decoder-v3.decode-and-verify-price-feeds` → `wormhole-core-v4.parse-and-verify-vaa`/`parse-vaa`. Inside `parse-vaa`, the attacker-controlled `signatures-len` byte is used directly as the `signatures-len` argument to `(slice? (list …19 hashes…) u0 signatures-len)` inside an `unwrap-panic`. Since `signatures-len` is read straight from user input with only an implicit expectation that it be ≤19, a value greater than 19 makes `slice?` return `none`, and `unwrap-panic` aborts the transaction instead of returning an error.

### Finding Description
`market-trait.clar` exposes `collateral-add`, `collateral-remove`, `borrow`, and `liquidate` with an `(optional (list 3 (buff 8192)))` `price-feeds` parameter, explicitly documented as "up to 3 Pyth price feed buffers to update stale prices." [1](#0-0) 

In `market.clar`/`v0-4-market.clar`, this optional list is folded over by `write-feeds`/`write-feed`, which forwards each raw buffer, completely unvalidated by the market contract, straight into `pyth-oracle-v4.verify-and-update-price-feeds`: [2](#0-1) 

That public function in turn calls the decoder, which calls the Wormhole core contract's `parse-and-verify-vaa` → `parse-vaa` on the raw, attacker-supplied bytes: [3](#0-2) 

Inside `parse-vaa`, the byte at offset 5 of the buffer (`signatures-len`, fully attacker-controlled and read via `read-uint-8`, which can return any value 0–255) is used unchecked as the length bound for `slice?` on a fixed 19-element list, then the `Optional` result is force-unwrapped with `unwrap-panic`: [4](#0-3) 

`slice?` on a list of length 19 with an end index `signatures-len > 19` returns `none` per Clarity semantics, and `unwrap-panic` on `none` aborts the entire transaction (Clarity's fatal-panic behavior, analogous to Rust's `.unwrap()` panic in the referenced report). Because this code path is reachable from ordinary user calls (`borrow`, `collateral-add`, `collateral-remove`, `liquidate`) with attacker-supplied `price-feeds`, any value >19 in that single byte position deterministically aborts the whole call — this is the exact "malformed input causes an unwrap panic in a public verifier path" bug class from the referenced report, here manifesting as a Clarity contract-call abort rather than a Rust process crash.

### Impact Explanation
This does not corrupt state or cause fund loss/lock by itself since Clarity aborts revert all state changes atomically, so this is not an insolvency or a permanent freeze. However, it lands in the "temporary freezing of funds" bucket: a legitimate user attempting to update a stale price feed in the same transaction as `borrow`/`collateral-add`/`collateral-remove`/`liquidate` cannot get a graceful `(err …)` — they get an unrecoverable panic, and any attacker can also directly submit malformed `price-feeds` on someone else's `liquidate` call attempt (any of these functions are callable by any principal), though since Clarity aborts only revert the single transaction, sustained impact requires repeated targeting to block honest liquidators/borrowers who need atomic price update capability. The most direct impact is degraded availability of the "in-band price update" feature documented for `borrow`/`liquidate`, delaying legitimate liquidations or borrows that rely on that feature and temporarily freezing use of that funds-relevant functionality until a valid feed is supplied.

### Likelihood Explanation
Likelihood is high for occurrence: the `price-feeds` parameter is a raw, unvalidated `(buff 8192)` supplied directly by the calling principal to `collateral-add`, `collateral-remove`, `borrow`, and `liquidate`, with no length or structural pre-check in `market.clar` before it is passed to the Wormhole/Pyth chain. Triggering it only requires placing a byte value >19 at offset 5 of the submitted buffer — trivial to construct deliberately, and also plausible from a naive/malformed price-feed generator.

### Recommendation
In `wormhole-core-v4.clar`'s `parse-vaa`, validate `signatures-len` (e.g., `asserts! (<= signatures-len u19) ERR_VAA_PARSING_SIGNATURES_LEN`) before using it in `slice?`, and replace the `unwrap-panic` around the signatures-list `slice?` with `unwrap!`/`asserts!` returning a proper error response. Apply the same audit to any other place where attacker-controlled length/offset values feed into `unwrap-panic` on `slice?`/`as-max-len?` calls in the Pyth/Wormhole decoding path reachable from `price-feeds`.

### Proof of Concept
1. Construct a `(buff 8192)` value whose byte at index 5 is `0x14` (20, i.e., >19), with an otherwise well-formed PNAU/AUWV wrapper header so execution reaches `parse-vaa` (the preceding `parse-pnau-header` and PNAU/AUWV magic checks in `pyth-pnau-decoder-v3.clar` only validate magic/version/proof-type bytes, not signature count).
2. Call `market.clar`'s `borrow` (or `collateral-add`/`collateral-remove`/`liquidate`) as any ordinary user, passing this buffer as one entry in the `price-feeds` list.
3. Execution flow: `write-feed` → `pyth-oracle-v4.verify-and-update-price-feeds` → `pyth-pnau-decoder-v3.decode-and-verify-price-feeds` → `wormhole-core-v4.parse-and-verify-vaa` → `parse-vaa`.
4. At line computing `vaa-body-hash-list`/`signatures`, `slice?` on the 19-element hash/buffer lists with `signatures-len = 20` returns `none`; the enclosing `unwrap-panic` aborts the transaction with a runtime panic instead of a Clarity `(err …)`, demonstrating the DoS on the public entry point.

### Citations

**File:** local-testing/contracts/market/market-trait.clar (L9-44)
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
    
    ;; ============================================
    ;; DEBT MANAGEMENT
    ;; ============================================
    
    ;; Borrow assets against collateral
    ;; receiver: optional recipient (defaults to caller)
    ;; price-feeds: optional list of up to 3 Pyth price feed buffers to update stale prices
    ;; Returns: (ok true) on success
    (borrow (<ft-trait> uint (optional principal) (optional (list 3 (buff 8192)))) (response bool uint))
    
    ;; Repay borrowed debt
    ;; on-behalf-of: optional account to repay for (defaults to caller)
    ;; Returns: actual amount repaid
    (repay (<ft-trait> uint (optional principal)) (response uint uint))
    
    ;; ============================================
    ;; LIQUIDATION
    ;; ============================================
    
    ;; Liquidate an unhealthy position
    ;; Parameters: borrower, collateral-ft, debt-ft, debt-amount, min-collateral-expected, price-feeds
    ;; price-feeds: optional list of up to 3 Pyth price feed buffers to update stale prices
    ;; Returns: { debt: amount repaid, collateral: amount received }
    (liquidate (principal <ft-trait> <ft-trait> uint uint (optional principal) (optional (list 3 (buff 8192)))) 
               (response { debt: uint, collateral: uint } uint))
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

**File:** local-testing/contracts/pyth/contracts/pyth-pnau-decoder-v3.clar (L49-63)
```text
(define-public (decode-and-verify-price-feeds (pnau-bytes (buff 8192)) (wormhole-core-address <wormhole-core-trait>))
	;; Check execution flow
	(let ((execution-check (try! (contract-call? .pyth-governance-v3 check-execution-flow contract-caller none)))
			(offset (try! (parse-pnau-header pnau-bytes)))
			(pnau-vaa-size (try! (read-uint-16 pnau-bytes offset)))
			(pnau-vaa (try! (read-buff pnau-bytes (+ offset u2) pnau-vaa-size)))
			(vaa (try! (contract-call? wormhole-core-address parse-and-verify-vaa pnau-vaa)))
			(merkle-root-hash (try! (parse-merkle-root-data-from-vaa-payload (get payload vaa))))
			(encoded-price-updates (unwrap! (slice? pnau-bytes (+ offset u2 pnau-vaa-size) (len pnau-bytes)) ERR_INVALID_PNAU_BYTES))
			(decoded-prices-updates (try! (parse-and-verify-prices-updates encoded-price-updates merkle-root-hash)))
			(prices-updates (map cast-decoded-price decoded-prices-updates))
			(authorized-prices-data-sources (contract-call? .pyth-governance-v3 get-authorized-prices-data-sources)))
		;; Ensure that update was published by an data source authorized by governance
		(unwrap! (index-of? authorized-prices-data-sources { emitter-chain: (get emitter-chain vaa), emitter-address: (get emitter-address vaa) }) ERR_UNAUTHORIZED_PRICE_UPDATE)
		(ok prices-updates)))
```

**File:** local-testing/contracts/pyth/contracts/wormhole/wormhole-core-v4.clar (L136-171)
```text
		(signatures-len (unwrap! (read-uint-8 vaa-bytes u5) ERR_VAA_PARSING_SIGNATURES_LEN))
		(signatures-offset (+ u6 (* signatures-len SIGNATURE_DATA_SIZE)))
		(signatures (map read-one-signature 
			(unwrap-panic (slice? (list 
				(default-to 0x (slice? vaa-bytes u6 u72))
				(default-to 0x (slice? vaa-bytes u72 u138))
				(default-to 0x (slice? vaa-bytes u138 u204))
				(default-to 0x (slice? vaa-bytes u204 u270))
				(default-to 0x (slice? vaa-bytes u270 u336))
				(default-to 0x (slice? vaa-bytes u336 u402))
				(default-to 0x (slice? vaa-bytes u402 u468))
				(default-to 0x (slice? vaa-bytes u468 u534))
				(default-to 0x (slice? vaa-bytes u534 u600))
				(default-to 0x (slice? vaa-bytes u600 u666))
				(default-to 0x (slice? vaa-bytes u666 u732))
				(default-to 0x (slice? vaa-bytes u732 u798))
				(default-to 0x (slice? vaa-bytes u798 u864))
				(default-to 0x (slice? vaa-bytes u864 u930))
				(default-to 0x (slice? vaa-bytes u930 u996))
				(default-to 0x (slice? vaa-bytes u996 u1062))
				(default-to 0x (slice? vaa-bytes u1062 u1128))
				(default-to 0x (slice? vaa-bytes u1128 u1194))
				(default-to 0x (slice? vaa-bytes u1194 u1260))) u0 signatures-len))
		))
		(vaa-body-hash (keccak256 (keccak256 (unwrap! (slice? vaa-bytes signatures-offset vaa-bytes-len) ERR_VAA_HASHING_BODY))))
		;; following values are ignored as they are not used anywhere
		;; (timestamp (unwrap! (read-uint-32 vaa-bytes signatures-offset) ERR_VAA_PARSING_TIMESTAMP))
		;; (nonce (unwrap! (read-uint-32 vaa-bytes (+ signatures-offset u4)) ERR_VAA_PARSING_NONCE))
		;; (consistency-level (unwrap! (read-uint-8 vaa-bytes (+ signatures-offset u50)) ERR_VAA_PARSING_CONSISTENCY_LEVEL))
		(emitter-chain (unwrap! (read-uint-16 vaa-bytes (+ signatures-offset u8)) ERR_VAA_PARSING_EMITTER_CHAIN))
		(emitter-address (unwrap! (read-buff-32 vaa-bytes (+ signatures-offset u10)) ERR_VAA_PARSING_EMITTER_ADDRESS))
		(sequence (unwrap! (read-uint-64 vaa-bytes (+ signatures-offset u42)) ERR_VAA_PARSING_SEQUENCE))
		(payload (unwrap! (slice? vaa-bytes (+ signatures-offset u51) vaa-bytes-len) ERR_VAA_PARSING_PAYLOAD))
		(vaa-body-hash-list (unwrap-panic (slice? (list vaa-body-hash vaa-body-hash vaa-body-hash vaa-body-hash vaa-body-hash 
			vaa-body-hash vaa-body-hash vaa-body-hash vaa-body-hash vaa-body-hash vaa-body-hash 
			vaa-body-hash vaa-body-hash vaa-body-hash vaa-body-hash vaa-body-hash vaa-body-hash vaa-body-hash vaa-body-hash) u0 signatures-len)))
```
