## Title
Inconsistent oracle staleness thresholds for the same DIA price feed (USDh vs zUSDh) causes spurious reverts / freezing of collateral operations - (File: `mainnet/contracts/proposals/mainnet/v0-init.clar`)

### Summary
The mainnet asset-initialization proposal registers two assets that consume the **same** DIA oracle feed (`USDH-DIA-KEY`) with **different** `max-staleness` thresholds: the underlying `USDH-TOKEN` is registered with `max-staleness: u1200` (20 minutes), while the paired `zUSDh` vault (`.v0-vault-usdh`) that wraps the exact same feed is registered with `max-staleness: MAX-STALENESS` (`u120`, 2 minutes). This is the same bug class as Olympus finding [M-18]: two price consumers of a single upstream feed enforce inconsistent freshness windows, so the tighter check can revert even when the looser (and presumably correctly-calibrated) check would accept the same data.

### Finding Description
`market.clar`'s `price-resolve` enforces freshness via `oracle-timestamp-fresh`, which compares the feed's `publish-time`/`timestamp` against a per-asset `max-staleness` value pulled from the asset's registered oracle config: [1](#0-0) 

Both `USDH-TOKEN` (asset id 8, the underlying debt asset) and `.v0-vault-usdh` (asset id 9, the `zUSDh` collateral vault share) are registered against the identical oracle source (`type: TYPE-DIA`, `ident: USDH-DIA-KEY`), yet with different staleness budgets: [2](#0-1) 

Because `price-resolve` is keyed by `{type, ident}` for monotonic-timestamp bookkeeping but validates freshness using the caller-specific `max-staleness` taken from `oracle-data` (i.e., from each asset's own registry entry), the same underlying DIA `publish-time` will pass the check for `USDH-TOKEN` (budget 1200s) while failing for `zUSDh` (budget 120s) whenever the DIA feed's actual update cadence exceeds 120 seconds — which is exactly why the underlying asset was deliberately given the larger 1200s budget. This mirrors the root cause in the Olympus report: two staleness checks over the same feed with inconsistent multipliers/thresholds, where the tighter one is unrealistically strict relative to the feed's real publish cadence.

### Impact Explanation
`zUSDh` (`.v0-vault-usdh`) is enabled as collateral in the live egroup configuration and its price is resolved via `price-resolve`/`get-asset-value` on every market entry point that touches a zUSDh position — supply, withdraw, borrow, repay, and liquidation — through `price-resolve`/`get-asset-value`/`find-and-resolve-asset-value`: [3](#0-2) 

If the DIA `USDh/USD` feed's real publish cadence is, as implied by the 1200s budget on the underlying asset, greater than 120 seconds, then any unprivileged user operation involving `zUSDh` collateral (deposits, withdrawals, borrows against it, or its liquidation) will revert with `ERR-ORACLE-INVARIANT` even though the equivalent operation on the underlying `USDH-TOKEN` succeeds with the very same oracle data. This is a temporary (and, if the feed cadence never tightens, effectively permanent) freezing of user funds held as `zUSDh` collateral — they cannot be withdrawn, and debt positions collateralized by `zUSDh` cannot be managed — which falls under the in-scope "temporary freezing of funds" (and potentially "permanent freezing of funds") impact category.

### Likelihood Explanation
This does not require any attacker action — it is triggered automatically whenever the DIA oracle's natural publish interval exceeds 120 seconds, which is the entire reason the underlying `USDH-TOKEN` entry was configured with a 1200-second budget in the same proposal. Any ordinary user calling supply/withdraw/borrow/repay/liquidate against `zUSDh` positions during such a window will have the transaction revert deterministically.

### Recommendation
Align the `max-staleness` values for all assets that share the same underlying oracle feed (`type`+`ident`), i.e., set `zUSDh`'s `max-staleness` to `u1200` to match `USDH-TOKEN`, or otherwise derive per-feed staleness from a single canonical source rather than allowing each asset registration to independently specify a threshold for a shared feed.

### Proof of Concept
1. DIA's `USDh/USD` feed publishes at its normal cadence (e.g., every ~600 seconds, consistent with why the underlying `USDH-TOKEN` was given a 1200s staleness budget).
2. 121+ seconds after the last DIA publish, a user calls `supply`/`withdraw`/`borrow`/`repay` on the market using `zUSDh` (`.v0-vault-usdh`, asset id 9) as collateral.
3. `price-resolve` is invoked with `max-staleness: u120` for the `zUSDh` oracle entry; `oracle-timestamp-fresh` computes `delta > 120` and the call reverts with `ERR-ORACLE-INVARIANT`, per [4](#0-3) .
4. The identical timestamp/publish-time, if used to resolve `USDH-TOKEN` (asset id 8, `max-staleness: u1200`), passes the freshness check without issue, confirming the inconsistency is solely due to the mismatched thresholds configured in [2](#0-1) .

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L365-395)
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

**File:** mainnet/contracts/proposals/mainnet/v0-init.clar (L123-129)
```text
    ;; Asset ID 8: USDh
    (try! (contract-call? .v0-assets insert USDH-TOKEN
      { type: TYPE-DIA, ident: (unwrap-panic (to-consensus-buff? USDH-DIA-KEY)), callcode: none, max-staleness: u1200 }))

    ;; Asset ID 9: zUSDh (vault-usdh)
    (try! (contract-call? .v0-assets insert .v0-vault-usdh
      { type: TYPE-DIA, ident: (unwrap-panic (to-consensus-buff? USDH-DIA-KEY)), callcode: (some CALLCODE-ZUSDH), max-staleness: MAX-STALENESS }))
```
