### Title
zstSTXbtc collateral price resolution always reverts due to missing `CALLCODE-ZSTSTXBTC` branch in `resolve-callcode` - (File: `mainnet/contracts/market/v0-4-market.clar`)

### Summary
`v0-4-market.clar` defines the oracle price-transformation constant `CALLCODE-ZSTSTXBTC` and registers `zstSTXbtc` (asset id `u11`) as a supported ztoken, but the dispatcher function that resolves callcodes (`resolve-callcode`) never contains a branch for that constant, so every price lookup for `zstSTXbtc` falls through to the error case.

### Finding Description
The market contract declares the asset ids and the callcode used for the vault-ststxbtc receipt token: [1](#0-0) 

and the callcode constant itself: [2](#0-1) 

However, the `resolve-callcode` function — the analog of the Cosmos SDK `RegisterInterfaces`/`RegisterCodec` dispatch table cited in the source report — only handles callcodes `CALLCODE-STSTX` through `CALLCODE-ZUSDH`, and has no branch for `CALLCODE-ZSTSTXBTC`, falling back to `ERR-ORACLE-CALLCODE`: [3](#0-2) 

By contrast, the `local-testing/contracts/market/market.clar` version of the same function does include this branch, confirming the mainnet copy is missing it: [4](#0-3) 

The mainnet deployment proposal actually registers `stSTXbtc`/`zstSTXbtc` in the asset registry and enables `zstSTXbtc` as collateral: [5](#0-4) 

Because `zstSTXbtc`'s asset entry in `v0-assets.clar` carries `oracle.callcode = (some CALLCODE-ZSTSTXBTC)`, every call to `price-resolve`/`price-multi-resolve` for an asset set that includes `zstSTXbtc` will invoke `resolve-callcode` with `0x06`, which is unhandled and returns `ERR-ORACLE-CALLCODE`: [6](#0-5) 

Since market entry points (borrow, repay, collateral withdrawal, health checks, liquidation) all rely on `price-multi-resolve`/`price-resolve` to compute a user's position value across held collateral/debt assets, any user who has deposited `zstSTXbtc` as collateral will have every subsequent market operation touching their position revert with `ERR-ORACLE-CALLCODE`.

### Impact Explanation
Any principal who supplies `zstSTXbtc` (via `vault-ststxbtc`) as collateral cannot have their position's health computed. This blocks:
- Withdrawal of their own collateral (funds frozen).
- Borrowing against it.
- Liquidation of unhealthy positions holding `zstSTXbtc` (liquidators cannot resolve price, so undercollateralized debt cannot be liquidated, risking protocol insolvency from unliquidatable bad debt).

This is a temporary freezing of funds (recoverable only via a DAO-driven contract upgrade adding the missing branch), and in the liquidation-blocking scenario risks protocol insolvency if positions go underwater and cannot be liquidated — both squarely in-scope impacts (temporary freezing of funds / insolvency), reached purely through ordinary use of `collateral-add`/`borrow` with `zstSTXbtc`, not through any DAO misconfiguration (the registry entries themselves are correct; the bug is the missing code branch in `market.clar`).

### Likelihood Explanation
This triggers deterministically and unconditionally the moment any user deposits `zstSTXbtc` as collateral and the market attempts to resolve its price — no attacker action or special preconditions are required beyond normal use of a listed, enabled collateral asset.

### Recommendation
Add the missing branch to `resolve-callcode` in `mainnet/contracts/market/v0-4-market.clar` to handle `CALLCODE-ZSTSTXBTC`, mirroring the local-testing implementation:
```clarity
(if (is-eq cc CALLCODE-ZSTSTXBTC) (resolve-ztoken p stSTXbtc)
ERR-ORACLE-CALLCODE)
```

### Proof of Concept
1. DAO executes `v0-init.clar`, registering `stSTXbtc` (id `u10`) and `zstSTXbtc` (id `u11`, `callcode: (some CALLCODE-ZSTSTXBTC)`), and enabling `zstSTXbtc` for collateral.
2. A user calls `collateral-add` with `zstSTXbtc`.
3. Any subsequent call requiring health-factor computation for that user's position (e.g., `borrow`, withdrawal, or a liquidator's `liquidate` call) triggers `price-multi-resolve` → `price-resolve` → `resolve-callcode` with `cc = 0x06`.
4. `resolve-callcode`'s if-chain has no match for `0x06` and returns `ERR-ORACLE-CALLCODE`, reverting the transaction — collateral is stuck and the position cannot be liquidated even if it becomes undercollateralized.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L27-29)
```text
(define-constant stSTXbtc u10)
(define-constant zstSTXbtc u11) ;; vault-ststxbtc
(define-constant ztokens (list zSTX zsBTC zstSTX zUSDC zUSDH zstSTXbtc))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L40-46)
```text
(define-constant CALLCODE-STSTX 0x00)
(define-constant CALLCODE-ZSTX 0x01)
(define-constant CALLCODE-ZSBTC 0x02)
(define-constant CALLCODE-ZSTSTX 0x03)
(define-constant CALLCODE-ZUSDC 0x04)
(define-constant CALLCODE-ZUSDH 0x05)
(define-constant CALLCODE-ZSTSTXBTC 0x06)
```

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

**File:** mainnet/contracts/market/v0-4-market.clar (L373-403)
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

(define-private (price-multi-resolve
  (data (list 64 { type: (buff 1), ident: (buff 32), callcode: (optional (buff 1)), max-staleness: uint }))
  (aids (list 64 uint)))
  (let ((init { output: (list), valid: true, aids: aids, idx: u0 })
        (response (fold iter-price-multi data init)))
    (asserts! (get valid response) ERR-ORACLE-MULTI)
    (ok (get output response))))
```

**File:** local-testing/contracts/market/market.clar (L371-380)
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

**File:** mainnet/contracts/proposals/mainnet/v0-init.clar (L131-145)
```text
    ;; Asset ID 10: stSTXbtc
    (try! (contract-call? .v0-assets insert STSTXBTC-TOKEN
      { type: TYPE-PYTH, ident: STX-FEED-ID, callcode: none, max-staleness: MAX-STALENESS }))

    ;; Asset ID 11: zstSTXbtc (vault-ststxbtc)
    (try! (contract-call? .v0-assets insert .v0-vault-ststxbtc
      { type: TYPE-PYTH, ident: STX-FEED-ID, callcode: (some CALLCODE-ZSTSTXBTC), max-staleness: MAX-STALENESS }))
    
    ;; sBTC is enabled for collateral as both underlying, and zsBTC allowing for rehypothecated and non-rehypothecated collateral
    (try! (contract-call? .v0-assets enable SBTC-TOKEN true))
    (try! (contract-call? .v0-assets enable .v0-vault-sbtc true))
    (try! (contract-call? .v0-assets enable SBTC-TOKEN false))

    ;; zstSTXbtc is enabled for collateral only, no underlying as collateral, no underlying debt
    (try! (contract-call? .v0-assets enable .v0-vault-ststxbtc true))
```
