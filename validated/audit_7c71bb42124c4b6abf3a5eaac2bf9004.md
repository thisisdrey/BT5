### Title
Immutable external oracle callcode dependencies (Pyth, DIA, stSTX ratio) create a single point of failure that can DoS core market operations - ([File: mainnet/contracts/market/v0-4-market.clar])

### Summary
Every price resolution in the market contract hard-codes calls to three external, third-party contracts — `pyth-storage-v4`, `dia-oracle`, and `block-info-nakamoto-ststx-ratio-v2` — via `call-pyth`, `call-dia`, and `call-ststx-ratio`. Any of these external contracts failing, being paused, being retired, or being upgraded to a new address will cause `price-resolve`/`price-multi-resolve` to revert for the affected asset, which in turn blocks every core market entry point that depends on price resolution (collateral add/remove, borrow, health checks, liquidation) — the exact "single external dependency call breaks core user flows" bug class described in the report.

### Finding Description
The market's price pipeline is:
`price-resolve` → `resolve-price-feed` (dispatches by `type`) → `call-pyth`/`call-dia` (external contract calls) → `resolve-callcode` → `resolve-ststx` (external contract call to `call-ststx-ratio`). [1](#0-0) 

`call-pyth` and `call-dia` directly `contract-call?` to fixed, hard-coded third-party contract principals (`SP1CGXWEAMG6P6FT04W66NVGJ7PQWMDAC19R7PJ0Y.pyth-storage-v4`, `SP1G48FZ4Y7JY8G2Z0N51QTCYGBQ6F4J43J77BQC0.dia-oracle`) and immediately `unwrap!` the response, reverting the entire caller's transaction (`ERR-ORACLE-PYTH` / `ERR-ORACLE-DIA`) if that call fails for any reason: [2](#0-1) 

Similarly, `resolve-ststx` unwraps `call-ststx-ratio`, which is itself a direct call to a hard-coded external contract `SP4SZE494VC2YC5JYG7AYFQ44F5Q4PYV7DVMDPBG.block-info-nakamoto-ststx-ratio-v2`: [3](#0-2) [4](#0-3) 

`price-resolve` propagates any of these `unwrap!`/`try!` failures upward via `try!`, causing the whole call to abort: [5](#0-4) 

`price-multi-resolve`/`iter-price-multi` batch-resolve prices for a list of assets and fail the entire batch (`ERR-ORACLE-MULTI`) if even one asset's resolution fails: [6](#0-5) 

These price feeds are written via `write-feeds`, called at the top of every core position-affecting entry point in the market — `collateral-add`, `collateral-remove`, `supply-collateral-add`, `collateral-remove-redeem`, borrow, repay, and liquidation health checks all pull price data before allowing the position update to proceed: [7](#0-6) [8](#0-7) 

Because these three principals (`pyth-storage-v4`, `dia-oracle`, `block-info-nakamoto-ststx-ratio-v2`) are hard-coded literal contract addresses baked into the immutable market contract (no config/registry indirection, no fallback, no try/catch degradation path), an outage, pause, deprecation, or contract-address migration on any one of them permanently blocks all operations touching that price feed — mirroring the reported Aave/Compound single-point-of-failure pattern where an external dependency's unavailability DoSes core user flows (deposit/withdraw analog = collateral add/remove, borrow, and liquidation here).

### Impact Explanation
This lands on the in-scope "temporary freezing of funds" impact class: if `pyth-storage-v4`, `dia-oracle`, or `block-info-nakamoto-ststx-ratio-v2` reverts, gets paused, is deprecated, or is redeployed to a new address (all plausible independent operational events for third-party infra outside Zest's control), then for any asset relying on that feed:
- Users cannot add or remove collateral (`collateral-add`, `collateral-remove-redeem`, `supply-collateral-add`).
- Users cannot borrow or have their positions revalued for repay/health checks.
- Liquidators cannot liquidate at-risk positions for that asset (positions become frozen even if undercollateralized), and normal users cannot exit their positions either.
Because DIA is used only for USDH and stSTX ratio is used only for stSTX/zstSTX-derived assets, an isolated failure would freeze operations restricted to positions holding those specific assets — still a temporary freeze of user funds for the affected accounts. If Pyth (`pyth-storage-v4`) fails, it affects STX, sBTC, stSTX-derived, and USDC feeds simultaneously (broadest blast radius, per the asset registrations seen in `v0-init.clar`), locking essentially the whole market's collateral-touching functions.

### Likelihood Explanation
Likelihood is non-trivial but not certain: it requires one of three specific external, third-party Stacks contracts to fail, pause, or be migrated. Third-party oracle/infra outages, contract upgrades (which change the deployed address), or temporary halts are realistic operational events over the life of an immutable contract — this exactly parallels the cited precedent (Aave pausing markets in Nov 2023). Because the market contract has no upgrade path or fallback feed mechanism for these hard-coded addresses, any such external event is fatal to the affected code paths until a DAO-executed contract migration (out of scope per the rules, but the root cause — lack of resilience/fallback in the price-resolution code itself — is in scope).

### Recommendation
- Avoid `unwrap!`/`try!`-cascading failures for third-party oracle calls; wrap external oracle calls so a single feed failure degrades gracefully (e.g., fall back to last-known-good price within a bounded staleness window, or allow the DAO to hot-swap the feed's contract principal without needing a full contract redeploy).
- Consider adding a registry/interface layer for oracle principals so a governance action can point to a new pyth/DIA/stSTX-ratio contract implementation without requiring a full market contract migration.
- Add pause-tolerant fallback logic (manual price override by DAO, or multiple redundant price sources) so collateral withdrawal/liquidation remains possible even when one upstream oracle is degraded.

### Proof of Concept
Conceptual (Clarity, no live PoC needed, matches the "no PoC required" acknowledgment in the source report):
1. Assume `pyth-storage-v4` (or `dia-oracle`, or `block-info-nakamoto-ststx-ratio-v2`) pauses, is retired, or reverts on `get-price`/`get-value`/`get-ststx-ratio-v3` for any reason.
2. Any user calls `collateral-add`, `supply-collateral-add`, `collateral-remove-redeem`, a borrow function, or a liquidator calls a liquidation function for an asset priced via that feed.
3. `write-feeds`/`price-resolve` → `call-pyth`/`call-dia`/`call-ststx-ratio` → `unwrap!` fails → `ERR-ORACLE-PYTH`/`ERR-ORACLE-DIA`/`ERR-ORACLE-CALLCODE` is returned, and the entire transaction reverts.
4. All such calls for the affected asset(s) now permanently fail until the external oracle contract is restored or the DAO redeploys/migrates the market to point at a new oracle address — freezing user positions in the interim.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L308-335)
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

(define-private (resolve-dia (ident (buff 32)))
  (let ((key (unwrap-panic (from-consensus-buff? (string-ascii 32) ident)))
        (res (try! (call-dia key))))
    ;; DIA returns timestamp in milliseconds, convert to seconds for staleness check
    (ok { value: (get value res), timestamp: (/ (get timestamp res) u1000) })))

(define-private (resolve-price-feed (type (buff 1)) (ident (buff 32)))
  (if (is-eq type TYPE-PYTH) (resolve-pyth ident)
  (if (is-eq type TYPE-DIA) (resolve-dia ident)
  ERR-ORACLE-TYPE)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L339-341)
```text
(define-private (resolve-ststx (p uint))
  (let ((ratio (unwrap! (call-ststx-ratio) ERR-ORACLE-CALLCODE)))
    (ok (mul-div-down p ratio STSTX-RATIO-DECIMALS))))
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

**File:** mainnet/contracts/market/v0-4-market.clar (L397-418)
```text
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

**File:** mainnet/contracts/market/v0-4-market.clar (L1014-1016)
```text
;; ststx ratio transformation
(define-public (call-ststx-ratio)
  (contract-call? 'SP4SZE494VC2YC5JYG7AYFQ44F5Q4PYV7DVMDPBG.block-info-nakamoto-ststx-ratio-v2 get-ststx-ratio-v3))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1020-1050)
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
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1175-1230)
```text
(define-public (supply-collateral-add (ft <ft-trait>) (amount uint) (min-shares uint) (price-feeds (optional (list 3 (buff 8192)))))
  (let ((ft-address (contract-of ft))
        (asset (try! (get-asset ft-address)))
        (asset-id (get id asset))
        (account contract-caller))
    
    ;; Preconditions
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)
    (asserts! (is-eq contract-caller tx-sender) ERR-AUTHORIZATION)
    
    ;; Step 1: Transfer underlying tokens from user to this contract (market)
    (try! (contract-call? ft transfer amount account current-contract none))
    
    ;; Step 2: Deposit to vault to get zTokens (minted to user)
    ;; Now the market has the underlying tokens and can call vault-deposit
    (let ((shares-minted 
            (try! (if (is-eq ft-address ZEST-STX-WRAPPER-CONTRACT)
              ;; For wSTX: use as-contract with-stx pattern
              (as-contract? ((with-stx amount))
                (try! (vault-deposit asset-id amount min-shares account)))
              ;; For other tokens: use as-contract with-ft pattern
              (as-contract? ((with-ft ft-address "*" amount))
                (try! (vault-deposit asset-id amount min-shares account)))))))
      
      ;; Step 3: Add the minted zTokens as collateral
      (if (is-eq asset-id STX) (collateral-add .v0-vault-stx shares-minted price-feeds)
      (if (is-eq asset-id sBTC) (collateral-add .v0-vault-sbtc shares-minted price-feeds)
      (if (is-eq asset-id stSTX) (collateral-add .v0-vault-ststx shares-minted price-feeds)
      (if (is-eq asset-id USDC) (collateral-add .v0-vault-usdc shares-minted price-feeds)
      (if (is-eq asset-id USDH) (collateral-add .v0-vault-usdh shares-minted price-feeds)
      (if (is-eq asset-id stSTXbtc) (collateral-add .v0-vault-ststxbtc shares-minted price-feeds)
      ERR-UNKNOWN-VAULT))))))))
)

;; -- Collateral-remove and redeem for withdrawing underlying from ztoken collateral

(define-public (collateral-remove-redeem (ft <ft-trait>) (amount uint) (min-underlying uint) (receiver (optional principal)) (price-feeds (optional (list 3 (buff 8192)))))
  (let ((ft-address (contract-of ft))
        (asset (try! (get-asset ft-address)))
        (ztoken-id (get id asset))
        (underlying-id (if (is-eq ztoken-id zSTX) STX
                       (if (is-eq ztoken-id zsBTC) sBTC
                       (if (is-eq ztoken-id zstSTX) stSTX
                       (if (is-eq ztoken-id zUSDC) USDC
                       (if (is-eq ztoken-id zUSDH) USDH
                       (if (is-eq ztoken-id zstSTXbtc) stSTXbtc
                       u100)))))))  ;; invalid sentinel for non-ztoken
        (funds-receiver (match receiver recv recv contract-caller)))

    (asserts! (<= underlying-id stSTXbtc) ERR-UNKNOWN-VAULT)
    
    ;; Step 1: Remove collateral - sends zTokens to THIS contract (market)
    ;; receiver=current-contract so market holds the zTokens
    (try! (collateral-remove ft amount (some current-contract) price-feeds))
    
    ;; Step 2: Redeem zTokens for underlying
```
