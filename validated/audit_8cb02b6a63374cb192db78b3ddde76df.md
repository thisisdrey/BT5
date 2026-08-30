### Title
No setter/migration path for hardcoded Pyth/DIA oracle contract addresses in `v0-4-market.clar`, causing permanent freeze of all price-dependent actions if either provider redeploys - (File: `mainnet/contracts/market/v0-4-market.clar`)

### Summary
`v0-4-market.clar` resolves all collateral/debt prices by calling hardcoded, literal principal addresses for the Pyth (`SP1CGXWEAMG6P6FT04W66NVGJ7PQWMDAC19R7PJ0Y.pyth-storage-v4`) and DIA (`SP1G48FZ4Y7JY8G2Z0N51QTCYGBQ6F4J43J77BQC0.dia-oracle`) oracles. Unlike `assets.clar`'s `oracle-data` (type/ident/callcode/staleness), which the DAO can update via `update`, the underlying oracle *contract addresses* themselves are baked into `call-pyth`/`call-dia` as compile-time literals with no configuration variable and no setter — mirroring the reported EthAnchor `exchangeRateFeeder` issue where an external dependency's address can change but the consuming contract has no mechanism to follow it.

### Finding Description
Price resolution is implemented via: [1](#0-0) 
which contract-calls the literal address `'SP1CGXWEAMG6P6FT04W66NVGJ7PQWMDAC19R7PJ0Y.pyth-storage-v4`, and: [2](#0-1) 
which calls the literal `'SP1G48FZ4Y7JY8G2Z0N51QTCYGBQ6F4J43J77BQC0.dia-oracle`. Both are used by `resolve-price-feed`: [3](#0-2) 

By contrast, the asset registry only stores oracle *type/ident/callcode/staleness*, not the oracle contract's address, and only the DAO-gated `update` function can change that metadata: [4](#0-3) 

There is no variable, map, or setter anywhere in `v0-4-market.clar` that stores the Pyth or DIA storage-contract principal — it is a Clarity literal embedded directly in `call-pyth`/`call-dia`. Because Clarity contracts are immutable once deployed, if Pyth or DIA (both actively-developed, externally operated protocols) deprecate/redeploy their storage contract to a new principal — exactly the kind of address-migration behavior flagged in the original EthAnchor report for `exchangeRateFeeder` — every call to `call-pyth`/`call-dia` will hit a stale/non-existent contract and fail with `ERR-ORACLE-PYTH` / `ERR-ORACLE-DIA`, propagating up through `resolve-price-feed` → `price-resolve` → `price-multi-resolve` used throughout deposit, borrow, withdraw, and liquidation health-check paths.

### Impact Explanation
Because price resolution failures hard-`unwrap!`/error out rather than degrading gracefully, and because there is no on-chain mechanism (not even a DAO-gated one) to repoint the oracle address, any address migration by Pyth or DIA permanently blocks every price-dependent market operation — deposits, withdrawals, borrows, and liquidations that require health checks or price-multi-resolve. Since Clarity contracts cannot be patched in place, users' collateral and debt positions in `v0-4-market.clar` would become frozen until a brand-new market contract is deployed and users manually migrate — an unbounded, uncontrolled freeze of user funds. This lands in the **High** impact category (temporary/permanent freezing of funds), and could escalate toward **Critical** (protocol insolvency-adjacent) if liquidations are also blocked while collateral value continues to move on other markets.

### Likelihood Explanation
Likelihood is tied entirely to an external, non-Zest event (Pyth or DIA redeploying/rotating their oracle contract on Stacks) — the same category of event the original report warned about for EthAnchor's `ExchangeRateFeeder`. This is not attacker-triggerable and requires no bug in Zest's arithmetic; the vulnerability is architectural: the absence of any indirection/setter for a dependency whose docs/behavior explicitly allow address changes. Given that both Pyth's Stacks bridge and DIA are actively maintained external integrations, non-trivial likelihood exists over the protocol's lifetime.

### Recommendation
Store the Pyth and DIA oracle contract principals as configurable state (e.g., DAO-gated data-vars in `v0-4-market.clar`, or route through a trait-based indirection contract) rather than as compiled-in literals, and add a DAO-authorized setter analogous to `assets.clar`'s `update`, so the market can be repointed to a new oracle deployment without redeploying/migrating the entire market contract and its position state.

### Proof of Concept
1. Pyth (or DIA) deprecates `SP1CGXWEAMG6P6FT04W66NVGJ7PQWMDAC19R7PJ0Y.pyth-storage-v4` and redeploys under a new principal, as EthAnchor's docs state can happen for `ExchangeRateFeeder`-class dependencies.
2. Any user calls a market function that needs a price (deposit, borrow, withdraw, liquidate) — this triggers `resolve-price-feed` → `call-pyth`, which `contract-call?`s the now-stale address.
3. The call to the deprecated/non-existent contract fails, and `call-pyth` returns `ERR-ORACLE-PYTH` (or `ERR-ORACLE-DIA` for DIA-based assets like USDH), causing the entire transaction to abort.
4. Because no setter exists to update the hardcoded principal in `v0-4-market.clar`, this failure is permanent for that deployed contract — every price-dependent action for every user is blocked until Zest deploys and migrates to an entirely new market contract. [5](#0-4)

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

**File:** mainnet/contracts/registry/v0-assets.clar (L218-250)
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
    
    (print {
      action: "asset-update",
      caller: tx-sender,
      data: {
        asset-address: asset,
        asset-id: asset-id,
        oracle-type: (get type oracle-data),
        oracle-ident: (get ident oracle-data),
        oracle-callcode: (get callcode oracle-data),
        max-staleness: staleness
      }
    })
    
    (ok true)
  ))
```
