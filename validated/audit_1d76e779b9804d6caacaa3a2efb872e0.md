### Title
Unrecoverable `unwrap-panic` on external oracle/vault-accrual calls inside `get-assets`/`accrue-user-collateral`/`accrue-user-debts` can permanently block liquidation of unhealthy positions - (File: `mainnet/contracts/market/v0-4-market.clar`)

### Summary
`market.clar` resolves collateral/debt prices and accrues vault indexes for every asset referenced by a position's bitmask before it can evaluate health or process a `liquidate` call. These internal helpers wrap the results of external contract calls (oracle reads, vault `accrue` calls) with `unwrap-panic` instead of propagating a graceful `(err ...)`. Any single failing external call — a stale/low-confidence Pyth or DIA price, or a vault `accrue` error — aborts the *entire* transaction rather than failing just for that asset. This mirrors the audited Kakarot finding: a called sub-component's failure/panic is not surfaced as a recoverable error to the caller, so higher-level logic that is supposed to gracefully skip or reject a single bad input instead bricks the whole operation.

### Finding Description
In `get-assets`, prices for every collateral asset relevant to a position are resolved and merged with `unwrap-panic`: [1](#0-0) 

`price-multi-resolve` ultimately calls `resolve-price-feed` → `resolve-pyth`/`resolve-dia`, which internally use `try!`/`unwrap!` to correctly propagate oracle errors as `(err ...)`: [2](#0-1) 

However, the caller (`get-assets`) discards that graceful error channel by calling `unwrap-panic` on the aggregate result, so a single stale timestamp, low-confidence reading, or oracle-call failure for *any* asset in the position's mask (including a disabled/inactive asset that merely happens to be part of the bitmask) causes the whole transaction to panic and abort — not just fail that one asset's evaluation.

The same pattern exists in the accrual helpers used before liquidation math: [3](#0-2) 

These `unwrap-panic (accrue-and-cache vault-id)` calls are invoked from `accrue-user-debts`/`accrue-user-collateral`, which are called directly inside `liquidate` before health/price evaluation: [4](#0-3) 

Notably, the contract even defines `ERR-DISABLED-COLLATERAL-PRICE-FAILED`, indicating the protocol's design intent was to *gracefully* tolerate price-resolution failures for certain assets rather than aborting the whole call: [5](#0-4) 

But that graceful-failure intent is defeated by the `unwrap-panic` calls in `get-assets`/`accrue-user-*`, which convert any single-asset oracle or vault-accrual hiccup into a hard panic for the entire call — exactly the "does not gracefully handle panics/failures in called sub-components" bug class from the reference report, adapted to Clarity's `unwrap-panic`-vs-`try!` distinction (the Clarity analog of a syscall that can only panic instead of returning a `Result`).

### Impact Explanation
Because `liquidate`, `borrow`, and health-check paths all route through `get-assets`/`accrue-user-debts`/`accrue-user-collateral`, any borrower whose position mask includes an asset with a currently stale/low-confidence oracle reading (which can legitimately occur for low-liquidity feeds, or any collateral the user chooses to hold) will cause **every** attempt to evaluate or liquidate that position to abort. An underwater borrower thus gains a mechanism to make their own position temporarily unliquidatable simply by holding (or being forced to hold, e.g. via a small residual dust balance) a collateral/debt asset whose price/accrual call currently fails, blocking liquidators from calling `liquidate` at all until the specific oracle/vault issue resolves. This is a temporary freezing of lender funds/yield (bad debt cannot be resolved via liquidation while it is trapped, and interest keeps accruing without collection), aligning with the in-scope **temporary freezing of funds** impact class tied to market health checks, oracle resolution, and position accounting.

### Likelihood Explanation
Likelihood is moderate: stale/low-confidence price conditions are a normal, expected occurrence for Pyth/DIA feeds (not attacker-caused market manipulation), and any account whose collateral/debt bitmask happens to include such an asset at the time of a `liquidate` or `borrow` call will trigger the panic deterministically. No DAO compromise or privileged access is needed — an ordinary borrower's own position composition is sufficient to reach this path.

### Recommendation
Replace `unwrap-panic` in `get-assets`, `accrue-debt-asset`, and `accrue-collateral-asset` with proper `try!`/`match` propagation that returns a recoverable `(err ...)` (e.g., `ERR-DISABLED-COLLATERAL-PRICE-FAILED` or a similar per-asset error) up to the caller, allowing callers such as `liquidate` to either skip pricing for a specific disabled/failing asset (as the existing error constant suggests was intended) or fail with a clear, catchable error instead of an unrecoverable panic that blocks the whole operation.

### Proof of Concept
1. Deploy/observe a position where an enabled collateral or debt asset's oracle feed (Pyth or DIA) becomes stale or its confidence exceeds `max-confidence-ratio` (a naturally occurring, non-attacker-controlled condition, or one the user can wait for/select for).
2. Have the position become unhealthy (standard liquidation trigger conditions).
3. Call `liquidate` on that borrower: `accrue-user-collateral`/`accrue-user-debts` (lines 271-293) and later `get-assets` (lines 482-492) will invoke `unwrap-panic` on the failing oracle/vault call, causing the entire `liquidate` transaction to abort rather than return `ERR-ORACLE-PYTH`/`ERR-ORACLE-DIA`/`ERR-DISABLED-COLLATERAL-PRICE-FAILED` gracefully.
4. Because every liquidation attempt against this position takes the same code path, no liquidator can successfully liquidate the position until the underlying oracle/vault issue resolves itself, temporarily freezing the lender-side funds tied up in the bad debt.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L86-86)
```text
(define-constant ERR-DISABLED-COLLATERAL-PRICE-FAILED (err u400020))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L271-293)
```text
  (fold accrue-collateral-asset coll-list { success: true }))

(define-private (accrue-collateral-asset
  (coll-entry { aid: uint, amount: uint })
  (acc { success: bool }))
  (let ((aid (get aid coll-entry)))
    ;; Only accrue if asset is a registered ztoken
    (if (is-ztoken aid)
        ;; ZToken: map to underlying vault routing ID and accrue
        ;; zSTX(1)->STX(0), zsBTC(3)->sBTC(2), zstSTX(5)->stSTX(4), zUSDC(7)->USDC(6), zUSDH(9)->USDH(8), zstSTXbtc(11)->stSTXbtc(10)
        (let ((vault-id (if (is-eq aid zSTX) STX
                        (if (is-eq aid zsBTC) sBTC
                        (if (is-eq aid zstSTX) stSTX
                        (if (is-eq aid zUSDC) USDC
                        (if (is-eq aid zUSDH) USDH
                        (if (is-eq aid zstSTXbtc) stSTXbtc
                        ;; will cause ERR-UNKNOWN-VAULT with any value over 64
                        u100))))))))
          (begin
            (unwrap-panic (accrue-and-cache vault-id))
            acc))
        ;; Non-ztoken: skip accrual (no liquidity index needed)
        acc)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L308-330)
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
```

**File:** mainnet/contracts/market/v0-4-market.clar (L482-492)
```text
(define-private (get-assets (mask-user uint))
  (let ((mask-enabled (get-enabled-bitmap))
        (safe-mask (user-safe-mask mask-user mask-enabled))
        (iter (mask-to-list-collateral safe-mask))
        (assets-list (get-status-multi iter))
        (oracles-list (map get-oracle assets-list))
        ;; Extract asset-ids for price resolution
        (asset-ids (map get-asset-id assets-list))
        ;; Use internal price resolution
        (prices-list (unwrap-panic (price-multi-resolve oracles-list asset-ids))))
    (map merge-price assets-list prices-list)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1403-1410)
```text
    (debt-aid (get id debt-asset))

    ;; accrue FIRST - populates cache for zToken price resolution
    (u-debt (accrue-user-debts (get debt pos-full)))
    (u-coll (accrue-user-collateral (get collateral pos-full)))

    ;; NOW safe to resolve prices (cache is populated)
    (assets (get-assets mask))
```
