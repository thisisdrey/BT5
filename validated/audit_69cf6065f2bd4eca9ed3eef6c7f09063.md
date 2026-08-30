Confirmed: `call-ststx-ratio` in `market.clar` / `v0-4-market.clar` is a bare passthrough to an external contract's `get-ststx-ratio-v3`, and `resolve-ststx` applies this ratio directly to price collateral/debt with **no staleness check whatsoever** — unlike the Pyth/DIA path, which enforces `oracle-timestamp-fresh` against a per-asset `max-staleness`. [1](#0-0) [2](#0-1) [3](#0-2) 

### Title
Missing staleness validation on stSTX ratio callcode enables stale-price exploitation of stSTX/zstSTX/zstSTXbtc collateral and debt valuation - (File: `mainnet/contracts/market/v0-4-market.clar`)

### Summary
`resolve-ststx` blindly trusts the ratio returned by the external `block-info-nakamoto-ststx-ratio-v2` contract's `get-ststx-ratio-v3` and applies it to price stSTX-derived assets, with no timestamp or staleness check comparable to the one enforced on Pyth/DIA feeds.

### Finding Description
Every direct oracle price resolved through `price-resolve` is validated for freshness via `oracle-timestamp-fresh`, comparing the feed's publish timestamp against a per-asset `max-staleness` configured in the asset registry [4](#0-3) . This mirrors exactly the mitigation recommended in the referenced report for `WrappedIbbtcEth`.

However, `resolve-ststx`, invoked via the `CALLCODE-STSTX` transform for `stSTX`, `zstSTX` (via `CALLCODE-ZSTSTX`), and indirectly for `stSTXbtc`/`zstSTXbtc` pricing paths, does not go through this pipeline at all:
```clarity
(define-private (resolve-ststx (p uint))
  (let ((ratio (unwrap! (call-ststx-ratio) ERR-ORACLE-CALLCODE)))
    (ok (mul-div-down p ratio STSTX-RATIO-DECIMALS))))

(define-public (call-ststx-ratio)
  (contract-call? 'SP4SZE494VC2YC5JYG7AYFQ44F5Q4PYV7DVMDPBG.block-info-nakamoto-ststx-ratio-v2 get-ststx-ratio-v3))
``` [1](#0-0) [2](#0-1) 

`call-ststx-ratio` returns only a bare `uint` ratio, with no accompanying timestamp, and no `last-update`/`max-staleness` check is performed anywhere in the callcode path (`resolve-callcode` → `resolve-ststx`) [5](#0-4) . If the external staking-ratio contract stops updating for any reason (its own bug, upstream dependency failure, or simply because the underlying stSTX protocol pauses updates), `market.clar` will keep using the last value it happens to read from that contract's storage indefinitely, with zero on-chain detection of staleness — exactly the root-cause condition the referenced report warns about for `pricePerShare`.

This is directly reachable from ordinary market entry points that resolve prices for collateral/debt valuation and liquidation health checks, e.g. `collateral-add`, `get-asset-value`, `process-collateral-asset`, and `liquidate`, all of which route through `price-resolve`/`resolve-callcode`/`resolve-ststx` for stSTX-derived assets [6](#0-5) .

### Impact Explanation
If the real STX staking ratio moves (accrues) after the external ratio contract stalls, an attacker can:
1. Supply stSTX/zstSTX as collateral or borrow against it while the market still uses the stale (lower) ratio, undervaluing their stSTX collateral relative to its true worth, or overvaluing/undervaluing stSTX debt depending on direction of drift.
2. Wait for the external ratio to resume updating and jump to the true value.
3. Exploit the resulting mispriced collateral/debt boundary — e.g., borrow the maximum against undervalued collateral pre-update, then have the position become massively over-collateralized post-update and withdraw excess value, or induce unfair liquidations/avoid deserved liquidations — extracting value from the protocol's collateral pool. This is a theft-of-funds-at-rest vector via mispriced collateral/debt, landing in the Critical impact category (direct theft of user/protocol funds via a bug in oracle resolution logic).

### Likelihood Explanation
The likelihood depends on the external `block-info-nakamoto-ststx-ratio-v2` contract stalling or lagging behind the true stSTX/STX ratio, which — per the referenced report's precedent — is a realistic operational risk for any externally-updated ratio/price value with no on-chain staleness enforcement. Every other price path in this codebase (Pyth, DIA) already treats this class of risk as material enough to require `max-staleness` enforcement, which underscores that its absence here is inconsistent and exploitable rather than a deliberate design decision.

### Recommendation
Apply the same staleness-protection pattern used for Pyth/DIA feeds to the stSTX ratio: track a last-update timestamp for the ratio (either sourced from the external contract if available, or bounded by tracking the last block/time this contract successfully changed) and enforce a `max-staleness` threshold before using it in `resolve-ststx`, rejecting price resolution (and any dependent collateral/debt operation) if the ratio is older than the threshold.

### Proof of Concept
1. External `block-info-nakamoto-ststx-ratio-v2` ratio stalls at value R0 (true off-chain ratio has since risen to R1 > R0) due to any malfunction of that contract.
2. Attacker calls `collateral-add`/`borrow` against zstSTX/stSTX collateral; `resolve-ztoken`/`resolve-ststx` prices it using stale R0, understating true collateral value but the attacker structures the trade to profit from the eventual repricing (e.g., borrows other assets up to the R0-based limit, positioning to gain when R1 is later applied and the position appears healthier/unhealthier than intended, or shorts the mispricing via collateral swap flows).
3. Once the external ratio contract updates to R1, `resolve-ststx` immediately reflects the jump with no smoothing or staleness rejection, letting the attacker realize the gap as risk-free profit, mirroring the `pricePerShare`-stall exploit in the referenced report.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L339-341)
```text
(define-private (resolve-ststx (p uint))
  (let ((ratio (unwrap! (call-ststx-ratio) ERR-ORACLE-CALLCODE)))
    (ok (mul-div-down p ratio STSTX-RATIO-DECIMALS))))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L349-358)
```text
(define-private (resolve-callcode (p uint) (callcode (optional (buff 1))))
  (let ((cc (unwrap! callcode (ok p))))
    (if (is-eq cc CALLCODE-STSTX) (resolve-ststx p)
    (if (is-eq cc CALLCODE-ZSTX) (resolve-ztoken p STX)
    (if (is-eq cc CALLCODE-ZSBTC) (resolve-ztoken p sBTC)
    (if (is-eq cc CALLCODE-ZSTSTX) (resolve-ztoken (try! (resolve-ststx p)) stSTX)
    (if (is-eq cc CALLCODE-ZUSDC) (resolve-ztoken p USDC)
    (if (is-eq cc CALLCODE-ZUSDH) (resolve-ztoken p USDH)
    (if (is-eq cc CALLCODE-ZSTSTXBTC) (resolve-ztoken p stSTXbtc)
    ERR-ORACLE-CALLCODE)))))))))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L365-410)
```text
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

(define-private (price-multi-resolve
  (data (list 64 { type: (buff 1), ident: (buff 32), callcode: (optional (buff 1)), max-staleness: uint }))
  (aids (list 64 uint)))
  (let ((init { output: (list), valid: true, aids: aids, idx: u0 })
        (response (fold iter-price-multi data init)))
    (asserts! (get valid response) ERR-ORACLE-MULTI)
    (ok (get output response))))

(define-private (iter-price-multi
  (oracle-data { type: (buff 1), ident: (buff 32), callcode: (optional (buff 1)), max-staleness: uint })
  (acc { output: (list 64 uint), valid: bool, aids: (list 64 uint), idx: uint }))
  (let ((valid (get valid acc))
        (skip? (asserts! valid acc))
        (asset-ids (get aids acc))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L668-687)
```text
(define-private (find-and-resolve-asset-value
                  (assets (list 64 
                    { id: uint, addr: principal, decimals: uint,
                    oracle: { type: (buff 1), ident: (buff 32), callcode: (optional (buff 1)), max-staleness: uint },
                    collateral: bool, debt: bool, price: uint }))
                  (asset-id uint) (amount uint) (round-up bool))
  (match (find-asset asset-id assets)
    asset (normalize (* amount (get price asset)) (get decimals asset) round-up)
    u0))

;; find-and-resolve-asset-value has "price" already pre-calculated, get-asset-value does not
(define-private (get-asset-value
                  (asset { id: uint, addr: principal, decimals: uint,
                          oracle: { type: (buff 1), ident: (buff 32), callcode: (optional (buff 1)), max-staleness: uint },
                          collateral: bool, debt: bool})
                  (amount uint) (round-up bool))
    (let ((oracle-data (get oracle asset))
          (price (try! (price-resolve oracle-data)))
          (decimals (get decimals asset)))
      (ok (normalize (* amount price) decimals round-up))))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1015-1016)
```text
(define-public (call-ststx-ratio)
  (contract-call? 'SP4SZE494VC2YC5JYG7AYFQ44F5Q4PYV7DVMDPBG.block-info-nakamoto-ststx-ratio-v2 get-ststx-ratio-v3))
```
