### Title
Hardcoded, non-upgradable external oracle contract addresses (`pyth-storage-v4`, `dia-oracle`, ststx-ratio contract) permanently DOS all borrow, collateral-remove-with-debt, and liquidation entry points if the external contract breaks - (File: `mainnet/contracts/market/v0-4-market.clar`)

### Summary
The Zest market contract resolves collateral/debt prices by making hardcoded `contract-call?`s to fixed external principal addresses for Pyth and DIA oracles, and for the ststx staking ratio. These addresses are baked into the contract logic as literals rather than being stored in an updatable data-var, so there is no on-chain mechanism (even via DAO) to redirect price resolution to a different address if the external dependency becomes broken. This mirrors the reported bug class: an unconditionally-trusted external registry/oracle whose failure (revert on every call) locks all code paths gated behind it.

### Finding Description
`call-pyth` and `call-dia` call fixed external principals: [1](#0-0) 

`call-ststx-ratio` similarly calls a hardcoded external principal used as a price callcode transform: [2](#0-1) 

These are invoked from `resolve-price-feed` and `resolve-callcode`, which are invoked from `price-resolve`/`price-multi-resolve`, the single price-resolution pipeline used throughout the market: [3](#0-2) [4](#0-3) 

`price-resolve`/`get-assets` is invoked from ordinary-principal entry points including `collateral-add` (when the user has existing debt), `collateral-remove` (when the user has debt or the collateral is disabled), and `borrow`: [5](#0-4) [6](#0-5) [7](#0-6) 

The `v0-assets` registry only allows the DAO to update the oracle `type`, `ident`, `callcode`, and `max-staleness` fields per asset — it never touches the actual contract address that `call-pyth`/`call-dia` target, because that address is a literal inside `market.clar`, not registry-configurable data: [8](#0-7) 

If the Pyth `pyth-storage-v4` contract, the DIA oracle contract, or the ststx-ratio contract reverts on every call (e.g., due to a bug or breaking upgrade in that external dependency, or the contract being paused/bricked), then `resolve-pyth`/`resolve-dia`/`resolve-ststx` will always return an error (`ERR-ORACLE-PYTH`, `ERR-ORACLE-DIA`, `ERR-ORACLE-CALLCODE`) via the `unwrap!` calls at those lines, and every function in the price-resolution chain (`price-resolve`, `price-multi-resolve`, `get-assets`, `get-notional-evaluation`) will revert. Since there is no data-var or admin function to repoint these external calls to a replacement contract, the DAO has no on-chain remedy other than a full contract migration/redeploy of `market.clar` — precisely the failure mode described in the report ("if this registry is broken and reverts on every transaction, all [gated] transfers will be locked").

### Impact Explanation
Any asset whose oracle `type` is Pyth or DIA, or that uses the `CALLCODE-STSTX`/ztoken-based callcodes tied to `call-ststx-ratio`, becomes unusable for: new borrowing (`borrow`), adding new collateral while having debt (`collateral-add`), and removing collateral while having debt or disabled collateral (`collateral-remove`) — since all these paths call into `price-resolve`. This is a temporary freezing of funds (users cannot access/adjust their locked collateral/debt positions) until a contract migration is performed, which falls under the in-scope "temporary freezing of funds" impact bucket (High). It does not affect `collateral-remove` for users with no debt, since that path explicitly skips price resolution.

### Likelihood Explanation
Likelihood depends on an external, non-Zest-controlled contract (Pyth storage, DIA oracle, or the block-info ststx-ratio helper) breaking or reverting — this is a dependency-failure scenario, not something an attacker can trigger unilaterally on a working oracle. However, unlike a normal price-staleness failure (which is an intentional safety control), a fully broken/reverting oracle dependency has zero on-chain mitigation path in this codebase: no fallback oracle, no updatable address, no circuit breaker that lets other price types keep functioning independently per-asset beyond what the registry already allows (type/ident/staleness only).

### Recommendation
Store the external oracle contract addresses (`pyth-storage-v4` address, `dia-oracle` address, ststx-ratio contract address) as DAO-updatable data-vars instead of hardcoded literals inside `market.clar`, and add a DAO-callable setter (analogous to `check-dao-auth`-gated setters elsewhere in the contract) so a broken dependency can be swapped without a full contract migration.

### Proof of Concept
1. Assume the external `pyth-storage-v4` contract (or `dia-oracle`, or the ststx-ratio contract) starts reverting unconditionally on every call — e.g., due to a bug introduced in an upgrade of that third-party contract, or it being paused/self-destructed by its own team.
2. Any user calls `borrow` on an asset whose registry entry (`v0-assets`) has `oracle.type` = `TYPE-PYTH` (or `TYPE-DIA`), or that requires the ststx ratio callcode.
3. `borrow` → `get-assets` → `price-multi-resolve` → `price-resolve` → `resolve-price-feed` → `call-pyth`/`call-dia` (or `resolve-callcode` → `resolve-ststx` → `call-ststx-ratio`) reverts with `ERR-ORACLE-PYTH`/`ERR-ORACLE-DIA`/`ERR-ORACLE-CALLCODE`.
4. The entire `borrow` transaction reverts. The same happens for `collateral-add` (with existing debt) and `collateral-remove` (with debt, or disabled collateral).
5. Because the target address is a Clarity literal inside `market.clar` rather than a stored variable, there is no DAO transaction (short of deploying and migrating to a new market contract) that can redirect these calls to a working oracle contract, so the lock persists until a full migration is completed.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L308-324)
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

(define-private (call-dia (key (string-ascii 32)))
  (let ((res (unwrap! (contract-call? 'SP1G48FZ4Y7JY8G2Z0N51QTCYGBQ6F4J43J77BQC0.dia-oracle get-value key) ERR-ORACLE-DIA)))
    (ok res)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L332-358)
```text
(define-private (resolve-price-feed (type (buff 1)) (ident (buff 32)))
  (if (is-eq type TYPE-PYTH) (resolve-pyth ident)
  (if (is-eq type TYPE-DIA) (resolve-dia ident)
  ERR-ORACLE-TYPE)))

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

**File:** mainnet/contracts/market/v0-4-market.clar (L373-418)
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

(define-private (iter-price-multi
  (oracle-data { type: (buff 1), ident: (buff 32), callcode: (optional (buff 1)), max-staleness: uint })
  (acc { output: (list 64 uint), valid: bool, aids: (list 64 uint), idx: uint }))
  (let ((valid (get valid acc))
        (skip? (asserts! valid acc))
        (asset-ids (get aids acc))
        (idx (get idx acc))
        ;; resolve price - will use cache for ztokens
        (price (unwrap! (price-resolve oracle-data) (merge acc { valid: false })))
        (next (unwrap-panic (as-max-len? (append (get output acc) price) u64))))
    { output: next,
      valid: true,
      aids: asset-ids,
      idx: (+ idx u1) }))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1014-1017)
```text
;; ststx ratio transformation
(define-public (call-ststx-ratio)
  (contract-call? 'SP4SZE494VC2YC5JYG7AYFQ44F5Q4PYV7DVMDPBG.block-info-nakamoto-ststx-ratio-v2 get-ststx-ratio-v3))

```

**File:** mainnet/contracts/market/v0-4-market.clar (L1020-1053)
```text
(define-public (collateral-add (ft <ft-trait>) (amount uint) (price-feeds (optional (list 3 (buff 8192)))))
  (let ((ft-address (contract-of ft))
        (asset (try! (get-asset ft-address)))
        (asset-id (get id asset))
        (account contract-caller))

    (asserts! (get collateral asset) ERR-COLLATERAL-DISABLED)
    (asserts! (is-eq contract-caller tx-sender) ERR-AUTHORIZATION)
    ;; Validate future mask has valid egroup AND check health if user has debt
    
    (match (contract-call? .v0-market-vault resolve-safe account)
      user-registry-data
        ;; User has existing position - check if adding NEW collateral asset
        (let ((current-raw-mask (get mask user-registry-data))
              (future-raw-mask (bit-or current-raw-mask (pow u2 asset-id)))
              (is-new-collateral (not (is-eq future-raw-mask current-raw-mask))))

          ;; If adding new collateral, validate egroup and check capacity
          (if is-new-collateral
              (let ((position (try! (get-position account)))
                    (current-mask (get mask position))
                    (future-mask (bit-or current-mask (pow u2 asset-id)))
                    (future-group (try! (get-egroup future-mask)))
                    ;; Accrue positions (required for price resolution)
                    (u-debt (accrue-user-debts (get debt position)))
                    (u-coll (accrue-user-collateral (get collateral position)))

                    ;; Get current egroup and notional values
                    (current-group (try! (get-egroup current-mask)))
                    (current-ltv (buff-to-uint-be (get LTV-BORROW current-group)))
                    (feeds-check (try! (write-feeds price-feeds)))
                    (current-assets (get-assets current-mask))
                    (current-notional (get-notional-evaluation { position: position, assets: current-assets }))
                    (current-debt-usd (get debt current-notional)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1107-1154)
```text
(define-public (collateral-remove (ft <ft-trait>) (amount uint) (receiver (optional principal)) (price-feeds (optional (list 3 (buff 8192)))))
  (let ((ft-address (contract-of ft))
        (asset (try! (get-asset ft-address)))
        (asset-id (get id asset))
        (account contract-caller)
        (collateral-receiver (match receiver recv recv contract-caller))
        (position (try! (get-position account)))
        (has-debt (> (len (get debt position)) u0)))

    (asserts! (> amount u0) ERR-AMOUNT-ZERO)

    (if has-debt
        ;; HAS DEBT: Full flow with price resolution and health checks
        (let ((is-collateral-enabled (get collateral asset))
              (feeds-check (try! (write-feeds price-feeds)))
              (position-mask (get mask position))
              (pos-full (if is-collateral-enabled position (try! (get-full-position account))))
              (u-debt (accrue-user-debts (get debt pos-full)))
              (u-coll (accrue-user-collateral (get collateral pos-full)))
              (assets (get-assets position-mask))
              (curr-coll-aid (find-collateral-amount (get collateral position) asset-id))
              (removing-all (is-eq amount curr-coll-aid))
              (current-group (try! (get-egroup position-mask)))
              (current-ltvb (buff-to-uint-be (get LTV-BORROW current-group)))
              (notional-valued-assets (get-notional-evaluation { position: position, assets: assets }))
              (collateral-value (get collateral notional-valued-assets))
              (debt-value (get debt notional-valued-assets))
              (removed-asset-value (find-and-resolve-asset-value assets asset-id amount true)))

          (asserts! (is-healthy collateral-value debt-value current-ltvb) ERR-UNHEALTHY)
          (asserts!
            (if is-collateral-enabled
                (let ((t (asserts! (>= collateral-value removed-asset-value) ERR-INSUFFICIENT-COLLATERAL))
                      (post-removal-collateral-value (- collateral-value removed-asset-value)))
                  (if removing-all
                      (let ((future-mask (bit-and position-mask (bit-not (pow u2 asset-id)))))
                        (try! (is-healthy-with-mask post-removal-collateral-value debt-value future-mask)))
                      (is-healthy post-removal-collateral-value debt-value current-ltvb)))
                (let ((oracle-data (get oracle asset))
                      (price (unwrap! (price-resolve oracle-data) ERR-DISABLED-COLLATERAL-PRICE-FAILED))
                      (decimals (get decimals asset))
                      (user-amount (find-collateral-amount (get collateral pos-full) asset-id))
                      (disabled-notional (normalize (* user-amount price) decimals false))
                      (removal-notional (normalize (* amount price) decimals true))
                      (total-collateral-value (+ collateral-value disabled-notional)))
                  (asserts! (>= total-collateral-value removal-notional) ERR-INSUFFICIENT-COLLATERAL)
                  (is-healthy (- total-collateral-value removal-notional) debt-value current-ltvb)))
            ERR-UNHEALTHY)
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1238-1260)
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
        
        ;; Step 2: Accrue user's positions (populates cache for ztokens)
        (u-debt (accrue-user-debts (get debt position)))
        (u-coll (accrue-user-collateral (get collateral position)))
        
        ;; Step 3: Accrue the asset being borrowed (needed for index access)
        (unused (accrue-and-cache asset-id))
        
        ;; Step 4: NOW safe to resolve prices (cache is populated)
        (assets (get-assets mask))

        ;; Calculate current health with current mask
```

**File:** mainnet/contracts/registry/v0-assets.clar (L218-235)
```text
(define-public (update
                (asset principal)
                (oracle-data {
                  type: (buff 1),
                  ident: (buff 32),
                  callcode: (optional (buff 1)),
                  max-staleness: uint
                }))
  (let ((entry (try! (find asset)))
        (asset-id (get id entry))
        (staleness (get max-staleness oracle-data))
        (updated-entry (merge entry { oracle: oracle-data })))

    (try! (check-dao-auth))
    (asserts! (> staleness u0) ERR-INVALID-STALENESS)

    (map-set registry asset-id updated-entry)
    
```
