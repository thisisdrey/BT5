### Title
DAO can accidentally set `max-confidence-ratio` too low, permanently bricking all Pyth price resolution and freezing every market entry point - (File: mainnet/contracts/market/v0-4-market.clar)

### Summary
The reported bug describes a scenario where two independently configurable parameters, when combined in a hardcoded `require`, can be accidentally set to a combination that always fails, permanently blocking a core user-facing entry point (`bid`). The Zest Protocol has a structurally analogous pattern in its Pyth oracle confidence check: `set-max-confidence-ratio` lets the DAO set `max-confidence-ratio` to any value `<= BPS` (including `0`), and this single value is later compared against live, externally-supplied Pyth confidence intervals in `check-confidence`. If set too low (in particular `0`, or any value below the confidence/price ratio that Pyth actually reports for a feed), every future `resolve-pyth` call unconditionally reverts with `ERR-PRICE-CONFIDENCE-LOW`, and since price resolution is required by essentially every market entry point (`borrow`, `repay`, `collateral-add`, `collateral-withdraw`, `liquidate`, health checks), the whole market freezes for that price feed until the DAO notices and corrects the parameter.

### Finding Description
`set-max-confidence-ratio` only validates an upper bound: [1](#0-0) 
There is no lower bound / sanity check ensuring the new ratio still permits real-world Pyth confidence intervals to pass.

This value is used directly in `check-confidence`, called from `resolve-pyth` on every Pyth-sourced price resolution: [2](#0-1) 

`check-confidence` asserts `confidence <= (price * max-confidence-ratio) / BPS` via `ERR-PRICE-CONFIDENCE-LOW`. Because Pyth virtually always returns some non-zero confidence interval for any live feed, setting `max-confidence-ratio` to `0` (or any value tighter than the feed's actual/typical confidence-to-price ratio) makes this assertion **unconditionally false** for that feed, exactly mirroring the reported pattern: a hardcoded comparison between two independently-configurable values (`max-confidence-ratio` set by governance, and the live confidence/price ratio delivered by the oracle) that the system can be "accidentally configured to never satisfy."

`resolve-pyth` is invoked from `price-resolve`, which is called from essentially every path that needs a USD valuation of an asset — `get-asset-value`, `get-notional-evaluation`, health checks in `borrow`, `repay`, `collateral-add`, `collateral-withdraw`, and `liquidate`: [3](#0-2) [4](#0-3) 

Because `price-resolve` reverts with `ERR-ORACLE-INVARIANT`/`ERR-PRICE-CONFIDENCE-LOW` whenever `check-confidence` fails, and because `borrow`/`repay`/`collateral-add`/`collateral-withdraw`/`liquidate` all call into `get-notional-evaluation`/`get-asset-value` to compute health, a misconfigured `max-confidence-ratio` blocks all of these operations for any asset priced by the affected Pyth feed — not just for a single caller, but for every ordinary user's normal transactions system-wide.

### Impact Explanation
This is a systemic denial-of-service on core lending flows (borrow, repay, collateral add/withdraw, liquidation) triggered by a single governance parameter set too aggressively. Per the impact taxonomy, this results in **temporary freezing of funds** — users cannot borrow against or withdraw their collateral, and liquidations of at-risk positions could also be blocked, until the DAO detects the misconfiguration and issues a corrective proposal (subject to multisig timelock delays documented in `dao-multisig.clar`). The severity is amplified in Zest because the market contract is the single consolidated hub for pricing, collateral, and debt (no separate oracle/router contract to work around), so one misconfigured ratio can halt the entire protocol's core flows for the affected asset(s).

### Likelihood Explanation
Likelihood is moderate: it requires a DAO governance action (via `dao-multisig`/`dao-executor`) to call `set-max-confidence-ratio` with a value that is too restrictive relative to real-world Pyth confidence data for a given feed. This is exactly the kind of "accidental misconfiguration" the original report describes — no malicious intent or DAO compromise is required, only a lack of sanity-checking against realistic oracle confidence data during a routine parameter update (e.g., tightening the ratio for security reasons without checking it against actual live feed behavior).

### Recommendation
Add a lower-bound sanity check in `set-max-confidence-ratio` (e.g., a protocol-defined minimum ratio, analogous to requiring `extendableUntil + SAFETY_WINDOW < expirationTime` be validated at configuration time rather than only checked at use-time). Additionally, consider validating the new ratio against the current/live confidence data for all configured Pyth feeds before committing the change, and/or providing an emergency fallback (e.g., temporarily falling back to DIA/mock feeds or allowing a bypass path) so a single bad oracle-confidence parameter cannot brick all market entry points simultaneously.

### Proof of Concept
1. DAO calls `set-max-confidence-ratio(u0)` (or any value below the live confidence/price ratio Pyth actually reports for a feed), which succeeds since the setter only checks `ratio <= BPS`. [1](#0-0) 
2. Any user calls `borrow`, `repay`, `collateral-add`, `collateral-withdraw`, or a liquidator calls `liquidate` for a position priced via that Pyth feed.
3. Internally, `get-asset-value` → `price-resolve` → `resolve-pyth` → `check-confidence` is executed: [2](#0-1) 
4. Because Pyth's returned `conf` is non-zero while `(price * 0) / BPS = 0`, the assertion `confidence <= 0` always fails, reverting the entire transaction with `ERR-PRICE-CONFIDENCE-LOW`.
5. Every subsequent call touching that asset's price fails the same way until the DAO submits and executes another proposal to raise `max-confidence-ratio` back to a workable value, during which time all affected borrow/repay/withdraw/liquidate operations are frozen.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L305-320)
```text
(define-private (check-confidence (price int) (confidence uint))
  (ok (asserts! (<= confidence (/ (* (to-uint price) (var-get max-confidence-ratio)) BPS)) ERR-PRICE-CONFIDENCE-LOW)))

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

**File:** mainnet/contracts/market/v0-4-market.clar (L1017-1032)
```text

;; -- Collateral operations --------------------------------------------------

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
