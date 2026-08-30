### Title
Hardcoded, non-upgradable calls to external oracle/ratio contracts (Pyth, DIA, stSTX ratio) can permanently freeze all market operations if those contracts are upgraded or replaced - (File: mainnet/contracts/market/v0-4-market.clar)

### Summary
`market.clar` resolves collateral/debt prices by making hardcoded `contract-call?`s to fixed, literal principal addresses for the Pyth storage contract, the DIA oracle, and the stSTX ratio provider. None of these dependencies are abstracted behind a trait or an upgradable pointer contract. If any of these external, third-party contracts is upgraded (address changes) or has its interface changed — which is a documented real-world occurrence for similar veAsset/oracle integrations — every price resolution call in `market.clar` reverts, and since price resolution is required for borrowing, repaying, liquidations, and health checks, the entire market freezes.

### Finding Description
`market.clar` embeds oracle resolution directly (no separate oracle contract, per design in `docs/market.md:422-427`), using hardcoded literal principals baked into the contract logic rather than DAO-updatable references: [1](#0-0) [2](#0-1) 

The stSTX ratio callcode transform (`resolve-ststx`) also depends on `call-ststx-ratio`, which per the analogous helper in `mainnet/contracts/utility/v0-1-data.clar:104-109` calls a hardcoded external principal `'SP4SZE494VC2YC5JYG7AYFQ44F5Q4PYV7DVMDPBG.block-info-nakamoto-ststx-ratio-v2`: [3](#0-2) 

These three external calls feed into `price-resolve` / `resolve-callcode`, which is on the critical path for every operation that requires a health check (borrow, withdraw collateral, liquidate): [4](#0-3) 

Because the target addresses (`.pyth-storage-v4`, `dia-oracle`, `block-info-nakamoto-ststx-ratio-v2`) are compiled directly into `market.clar`'s bytecode as literal principals rather than being read from a DAO-controlled data-var or a trait-typed argument supplied by the caller, there is no way to swap the target contract if the upstream provider deploys a new version with a changed interface (e.g., changed return tuple, renamed function, or address migration) — exactly the failure mode described in the referenced report for Angle's upgradable `LiquidityGaugeV4`. Any `unwrap!`/`unwrap-panic` on a call to a stale or interface-incompatible address will cause `ERR-ORACLE-PYTH`, `ERR-ORACLE-DIA`, or `ERR-ORACLE-CALLCODE` to be returned (or an outright runtime abort for the `unwrap-panic` paths), which propagates up and reverts the whole transaction.

### Impact Explanation
Since `price-resolve`/`resolve-callcode` sit on the mandatory path for health checks used in `borrow`, `collateral-remove`/withdrawal, and `liquidate` flows, an interface or address change on any one of Pyth storage, DIA oracle, or the stSTX ratio provider makes it impossible to compute health for any position holding the affected asset (and, for `stSTX`/`zstSTXbtc`, the ratio dependency taints the ztoken callcode chain too). This blocks legitimate users from repaying debt, withdrawing/adding collateral under health constraints, and blocks liquidators from liquidating unhealthy positions — a temporary (but potentially indefinite, until DAO redeploys/migrates the market) freezing of user funds. This lands squarely in the in-scope "temporary freezing of funds" impact class, matching the medium-severity classification of the referenced report.

### Likelihood Explanation
This does not require any bug on Zest's part to be triggered — it only requires a routine upgrade of one of three named third-party contracts, which is outside Zest's control and has historically happened to comparable oracle/ratio-provider integrations. Given the protocol has already hardcoded three separate external dependencies this way (Pyth storage, DIA oracle, stSTX ratio provider) across `v0-4-market.clar` and the `v0-1-data.clar` utility, the surface area for this failure mode is non-trivial, and likelihood is best characterized as low-to-moderate but with high blast radius once triggered.

### Recommendation
Route all external oracle/ratio dependencies through DAO-updatable data-vars (or trait-typed function arguments passed by the DAO-controlled proposal/init flow) instead of hardcoding literal principals inside `market.clar`'s private functions. This mirrors the recommended mitigation in the referenced report: either make the price-resolution "VoterProxy-equivalent" (i.e., `call-pyth`/`call-dia`/`call-ststx-ratio`) upgradable via a DAO-settable address, or introduce a thin intermediate adapter contract per external dependency that the DAO can redeploy/point-update without needing to migrate the entire `market.clar` hub.

### Proof of Concept
1. Pyth (or DIA, or the stSTX ratio provider) deploys a new version of their storage/oracle contract at a new principal, or changes the return tuple/field names of `get-price` / `get-value` / `get-ststx-ratio-v3` (as happened with Angle's `LiquidityGaugeV4UpgradedToken` referenced in the report).
2. `call-pyth` in `market.clar` (`mainnet/contracts/market/v0-4-market.clar:308-310`) continues calling the old hardcoded principal `'SP1CGXWEAMG6P6FT04W66NVGJ7PQWMDAC19R7PJ0Y.pyth-storage-v4`, which is now stale/deprecated or has an incompatible interface.
3. Every call to `price-resolve` for that asset now fails with `ERR-ORACLE-PYTH` (or equivalent), and since `price-resolve` is required to compute health for borrow/withdraw/liquidate, all such user-facing operations for positions touching that asset revert.
4. Users cannot repay, withdraw collateral respecting health, or be liquidated for the affected asset until the DAO executes a full market redeploy/migration — the only way to update the hardcoded principal is to ship a new `market.clar` version, exactly the "painful shutdown" scenario described in the report.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L308-310)
```text
(define-private (call-pyth (ident (buff 32)))
  (let ((res (unwrap! (contract-call? 'SP1CGXWEAMG6P6FT04W66NVGJ7PQWMDAC19R7PJ0Y.pyth-storage-v4 get-price ident) ERR-ORACLE-PYTH)))
    (ok res)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L322-324)
```text
(define-private (call-dia (key (string-ascii 32)))
  (let ((res (unwrap! (contract-call? 'SP1G48FZ4Y7JY8G2Z0N51QTCYGBQ6F4J43J77BQC0.dia-oracle get-value key) ERR-ORACLE-DIA)))
    (ok res)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L339-395)
```text
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

;; -- Oracle: price resolution -----------------------------------------------

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

**File:** mainnet/contracts/utility/v0-1-data.clar (L104-109)
```text
;; -- Oracle: stSTX ratio ----------------------------------------------------

;; Get stSTX/STX ratio (how much STX per stSTX)
;; Returns ratio in STSTX-RATIO-DECIMALS precision (1000000)
(define-private (get-ststx-ratio)
  (contract-call? 'SP4SZE494VC2YC5JYG7AYFQ44F5Q4PYV7DVMDPBG.block-info-nakamoto-ststx-ratio-v2 get-ststx-ratio-v3))
```
