## Analog Found

### Title
External `ststx` ratio oracle callcode trusts an external protocol call with no pause/staleness validation, enabling under-collateralized borrows that get socialized as bad debt onto all zSTX/zstSTX vault depositors - (File: `mainnet/contracts/market/v0-4-market.clar`)

### Summary
The Illuminate bug is about accepting a value/price signal from an external protocol (Pendle) without checking whether that protocol is paused or has become worthless, letting bad positions dilute other users' shares of pooled funds. Zest v2 has a structurally identical trust gap in its oracle `callcode` system: the price of `stSTX`/`zstSTX` collateral is derived by multiplying the Pyth STX/USD price by a ratio fetched from an external StackingDAO contract (`call-ststx-ratio`), and this ratio call carries none of the staleness/legality protections that are applied to the base Pyth/DIA price.

### Finding Description
`resolve-callcode` dispatches `CALLCODE-STSTX` and `CALLCODE-ZSTSTX` to `resolve-ststx`, which fetches the STX-per-stSTX exchange ratio from an external contract via `call-ststx-ratio` and multiplies it into the price: [1](#0-0) 

`call-ststx-ratio` performs a bare cross-contract call to a third-party, DAO-external contract (`block-info-nakamoto-ststx-ratio-v2`, owned/operated by StackingDAO, not Zest) with no check that this protocol is paused, halted, in an emergency state, or reporting stale/invalid data: [2](#0-1) 

Crucially, `price-resolve` only validates freshness/legality of the *base* oracle price's own `timestamp` (from Pyth/DIA), not of the callcode-transformed result: [3](#0-2) 

Because the ratio has no independent timestamp or staleness bound, if the external StackingDAO ratio contract is paused, halted, returns a frozen/incorrect ratio, or the ratio "protocol" is otherwise compromised, the only defense is `oracle-price-legal`, which merely checks `price > 0`. This is exactly the class of bug described in the analog report: "no checks that the protocol of the external [asset] is paused or has any value." The mainnet code even shows the safeguard was reduced further versus local-testing — the mainnet `call-ststx-ratio` makes the live external call unconditionally, whereas the local-testing version is stubbed to a constant, underscoring that the production path relies entirely on the external contract behaving correctly with no on-chain circuit breaker specific to that dependency: [4](#0-3) 

The resulting price feeds directly into `collateral-add`'s capacity check and into `get-notional-evaluation`/health checks used for borrowing and liquidation, so a stale/incorrect ratio inflates the USD value of `stSTX`/`zstSTX` collateral, letting an account borrow more `USDC`/`USDH`/etc. than the real collateral supports: [5](#0-4) 

When such an over-borrowed position is later liquidated and found to have no collateral left, the market explicitly falls back to socializing the bad debt across the corresponding vault's other depositors via `socialize-debt-asset` → `vault-socialize-debt` → the vault's `socialize-debt`, which writes down the vault's `lindex` (shrinking the value of every zToken holder's share) rather than making the borrower whole: [6](#0-5) [7](#0-6) 

This is the direct analog of the Illuminate finding: an unpaused/worthless-signal from an integrated external protocol is trusted without a protocol-level pause/health check, and the resulting bad position is socialized onto other users' shares (other zToken holders bear the loss on a per-share basis, exactly as Illuminate PT holders would).

### Impact Explanation
This lands on **Critical – protocol insolvency / direct theft of user funds at rest**, per the allowed impact classes. If the external stSTX ratio provider is paused, halted, or manipulated (e.g., during a StackingDAO incident), positions can be opened or maintained using an inflated stSTX/zstSTX valuation. Liquidation of such a position that ends up with no remaining collateral triggers `socialize-debt-asset`, which mutates the debt-asset vault's `lindex` downward, permanently and involuntarily diluting the redeemable value of every existing zToken holder in that vault — a loss socialized across unrelated users who never took the risk.

### Likelihood Explanation
Likelihood is **conditional on the external StackingDAO ratio contract's own liveness/correctness**, the same dependency structure Sherlock accepted as Medium-severity for the Illuminate analog. The Zest code makes no attempt to detect or react to that external protocol being paused/compromised — there is no staleness bound, no sanity/deviation check against the previous ratio, and no admin circuit breaker specific to the ratio feed (only the generic asset-level `enable`/pause mechanisms which require manual admin action after the fact, mirroring exactly the "no guarantee the admin notices before automated tools exploit it" argument made in the original report).

### Recommendation
Treat the `call-ststx-ratio` (and any other external-protocol-derived callcode, e.g. future `stSTXbtc` provider integrations) result the same way Pyth/DIA prices are treated: attach and enforce its own timestamp/staleness bound, bound the maximum per-call deviation from the last accepted ratio, and/or add an explicit health/pause check against the external StackingDAO contract before using its ratio in `resolve-ststx`/`resolve-callcode`. Consider allowing the DAO to configure a fallback/frozen-ratio circuit breaker independent of manual asset pausing.

### Proof of Concept
1. External StackingDAO ratio contract (`block-info-nakamoto-ststx-ratio-v2`) enters a paused/frozen/compromised state and returns a stale or manipulated (but non-zero) ratio value.
2. `call-ststx-ratio` at `mainnet/contracts/market/v0-4-market.clar:1015-1016` (or `local-testing/contracts/market/market.clar:1037-1039`) returns this value with no legality check beyond non-zero.
3. `resolve-ststx`/`resolve-callcode` folds this ratio into the stSTX/zstSTX USD price used in `collateral-add`'s capacity check (`mainnet/contracts/market/v0-4-market.clar:1055-1077`) and in health checks for borrowing.
4. An attacker opens a large borrow position against `stSTX`/`zstSTX` collateral valued using the inflated/incorrect ratio.
5. Once the ratio corrects (or is manually fixed), the position is undercollateralized; liquidation empties the collateral and the remaining debt is written off via `socialize-debt-asset` (`mainnet/contracts/market/v0-4-market.clar:1534-1560`), calling the debt vault's `socialize-debt` (`mainnet/contracts/vault/v0-vault-stx.clar:942-967`), which reduces `lindex` and thus the redeemable value for every other zToken holder of that vault.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L337-358)
```text
;; -- Oracle: callcode transformations ---------------------------------------

(define-private (resolve-ststx (p uint))
  (let ((ratio (unwrap! (call-ststx-ratio) ERR-ORACLE-CALLCODE)))
    (ok (mul-div-down p ratio STSTX-RATIO-DECIMALS))))

(define-private (resolve-ztoken (p uint) (aid uint))
  (let ((cached (unwrap! (get-cached-indexes aid) ERR-ORACLE-CALLCODE))
        (cached-lindex (get lindex cached))
        (scaled (* p cached-lindex)))
    (ok (div-down scaled INDEX-PRECISION))))

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

**File:** mainnet/contracts/market/v0-4-market.clar (L1055-1077)
```text
                ;; ONLY check capacity if user has debt
                (if (> current-debt-usd u0)
                    ;; Calculate future mask and validate egroup exists
                    (let ((current-coll-usd (get collateral current-notional))
                          (current-capacity (* current-coll-usd current-ltv))
                          ;; Prime cache for new zToken collateral underlying if not already cached
                          (cache-primed (if (is-ztoken asset-id)
                                            (let ((vault-id (if (is-eq asset-id zSTX) STX
                                                            (if (is-eq asset-id zsBTC) sBTC
                                                            (if (is-eq asset-id zstSTX) stSTX
                                                            (if (is-eq asset-id zUSDC) USDC
                                                            (if (is-eq asset-id zUSDH) USDH
                                                            (if (is-eq asset-id zstSTXbtc) stSTXbtc
                                                            u100))))))))
                                              (try! (accrue-and-cache vault-id)))
                                            { index: u0, lindex: u0 }))
                          (added-collateral-value (try! (get-asset-value asset amount false)))
                          (future-ltv (buff-to-uint-be (get LTV-BORROW future-group)))
                          (future-coll-usd (+ current-coll-usd added-collateral-value))
                          (future-capacity (* future-coll-usd future-ltv)))
                      ;; CRITICAL CHECK: Future capacity must not decrease
                      (asserts! (>= future-capacity current-capacity) ERR-UNHEALTHY))
                    ;; No debt - skip capacity check
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1534-1560)
```text
      ;; Handle bad debt socialization if no collateral left
      (let ((bad-debt-socialized 
              (if no-collateral-left
                  (let ((stripped-debt-list (filter-out-debt-asset (get debt pos-full) debt-aid))
                        (fresh-debt-list (if (is-eq debt-updated u0)
                                             stripped-debt-list
                                             (unwrap-panic (as-max-len?
                                               (append stripped-debt-list
                                                       { aid: debt-aid, scaled: debt-updated })
                                               u64)))))
                    (if (> (len fresh-debt-list) u0) ;; if still has debt
                      (let ((socialization-result (fold socialize-debt-asset 
                                                        fresh-debt-list 
                                                        { borrower: borrower, success: true })))
                        (asserts! (get success socialization-result) ERR-BAD-DEBT-SOCIALIZATION-FAILED)
                        ;; emit bad-debt-socialized event
                        (print {
                          action: "bad-debt-socialized",
                          caller: contract-caller,
                          data: {
                            borrower: borrower,
                            debt-list: fresh-debt-list
                          }
                        })
                        true)
                      false))
                  false)))
```

**File:** local-testing/contracts/market/market.clar (L395-417)
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

**File:** local-testing/contracts/market/market.clar (L1034-1039)
```text
;; -- Oracle (public call for ststx ratio) -----------------------------------

;; ststx ratio transformation
(define-public (call-ststx-ratio)
  ;; @mainnet: (contract-call? 'SP4SZE494VC2YC5JYG7AYFQ44F5Q4PYV7DVMDPBG.block-info-nakamoto-ststx-ratio-v2 get-ststx-ratio-v3))
  (ok STSTX-RATIO-DECIMALS))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L942-967)
```text
    (ok true)))

(define-public (socialize-debt (scaled-amount uint))
  (let ((scaled-principal (var-get principal-scaled))
        (borrowed (var-get total-borrowed))
        (idx (var-get index))
        (current-assets (var-get assets))
        (current-lindex (var-get lindex))
        (old-total-assets (total-assets))
        (debt-reduction (mul-div-down scaled-amount idx INDEX-PRECISION))
        (principal-reduction (if (> scaled-principal u0)
                                (mul-div-down scaled-amount borrowed scaled-principal)
                                u0))
        ;; Write down lindex proportionally to loss in total-assets
        (new-lindex (if (and (> old-total-assets u0) (> old-total-assets debt-reduction))
                       (mul-div-down current-lindex (- old-total-assets debt-reduction) old-total-assets)
                       u0)))

    (try! (check-caller-auth))
    (asserts! (> scaled-amount u0) ERR-AMOUNT-ZERO)

    (var-set lindex new-lindex)
    (var-set principal-scaled (if (> scaled-principal scaled-amount) (- scaled-principal scaled-amount) u0))
    (var-set total-borrowed (if (> borrowed principal-reduction) (- borrowed principal-reduction) u0))
    (var-set assets (if (> current-assets principal-reduction) (- current-assets principal-reduction) u0))

```
