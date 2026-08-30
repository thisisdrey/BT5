### Title
Withdrawal/borrow/liquidation DoS via unconditional external `ststx-ratio` contract call in oracle price resolution — permanent freezing of stSTX/zstSTX-collateralized positions ([File: mainnet/contracts/market/v0-4-market.clar])

### Summary
Any market operation that needs to compute a position's notional value (collateral withdrawal, borrow, liquidation) for a user holding stSTX or zstSTX collateral/debt must call the external stSTX ratio contract via `call-ststx-ratio()`. This call is hard-wired and unconditional — there is no fallback, cached value, or way to bypass it. If that external contract ever reverts, is deprecated, or is shut down/replaced (the exact analog of the vlCVX-shutdown scenario in the referenced report), every position touching stSTX/zstSTX becomes permanently unable to withdraw collateral, repay, or be evaluated at all, freezing user funds.

### Finding Description
`market.clar`'s oracle callcode system applies a price transformation for stSTX-denominated assets: [1](#0-0) [2](#0-1) 

`resolve-ststx` unconditionally `unwrap!`s the result of `call-ststx-ratio`, which is a live `contract-call?` out to `'SP4SZE494VC2YC5JYG7AYFQ44F5Q4PYV7DVMDPBG.block-info-nakamoto-ststx-ratio-v2`. Any revert, trap, or removal/upgrade of that dependency causes `resolve-ststx` — and therefore `resolve-callcode`, `price-resolve`, and `get-notional-evaluation` — to propagate the failure with `ERR-ORACLE-CALLCODE`.

This price-resolution path is reached from `collateral-remove` for any user with existing debt, whenever their position mask includes stSTX (`CALLCODE-STSTX`) or zstSTX (`CALLCODE-ZSTSTX`, which itself also calls `resolve-ststx`): [3](#0-2) 

The same `resolve-ststx`/`call-ststx-ratio` dependency is also reached by `borrow`, `collateral-add` (when the user has debt), and `liquidate`/`liquidate-redeem`, since all of them compute notional value via `get-notional-evaluation` over the full position mask.

This is structurally identical to the referenced Asymmetry/Votium finding: a withdrawal-critical code path makes an unconditional call into an external, DAO/third-party-controlled protocol contract whose only failure mode (shutdown, deprecation, revert) is outside Zest's control, yet Zest's own withdrawal logic has no guard, fallback, or bypass for that failure.

### Impact Explanation
If `block-info-nakamoto-ststx-ratio-v2` ever reverts or stops functioning (deprecated, migrated, paused by its own admin, or simply removed/replaced without a corresponding update to Zest, which per the rules is an "accidental DAO update" scenario only if Zest's DAO caused it — here the failure originates in the third-party contract itself, not a Zest DAO misconfiguration), **every** user with a debt position that includes stSTX or zstSTX collateral is permanently unable to:
- Withdraw any collateral (`collateral-remove`)
- Borrow more or top up collateral (`borrow`, `collateral-add`)
- Be liquidated (`liquidate`, `liquidate-redeem`), meaning even bad debt cannot be resolved

Because collateral-remove for debt-free stSTX depositors is unaffected (the "NO DEBT" branch skips price resolution), but any user who ever takes on debt against stSTX/zstSTX collateral is locked out entirely with no recovery path. This is a permanent freezing of user funds, matching the in-scope Critical/High impact class ("permanent freezing of funds").

### Likelihood Explanation
The likelihood depends on the external `block-info-nakamoto-ststx-ratio-v2` contract's own lifecycle. Reachability from an ordinary principal is trivial — any borrower with stSTX/zstSTX collateral calling `collateral-remove`, `borrow`, or being liquidated triggers the vulnerable path with no special conditions. The root cause is exclusively in Zest's own oracle-resolution code (`resolve-ststx`/`call-ststx-ratio`) lacking any defensive handling (e.g., cached last-known ratio, staleness fallback, or ability to disable the callcode transform for existing positions), so it qualifies as an in-scope bug in this codebase rather than a third-party depeg/data issue.

### Recommendation
Add resilience to `resolve-ststx`/`call-ststx-ratio` so that a revert or failure of the external ratio contract does not brick withdrawal/borrow/liquidation paths for existing positions, for example:
- Cache the last successfully observed stSTX ratio (with a staleness bound) and fall back to it if the live call fails/traps.
- Provide a DAO-gated emergency override to freeze the ratio at its last known value or to disable the callcode transform for withdrawal/liquidation-only paths, ensuring users can still exit and bad debt can still be liquidated even if the upstream contract is unavailable.
- At minimum, ensure `liquidate`/`liquidate-redeem` and `collateral-remove` withdrawal legs are guarded to degrade gracefully instead of reverting outright when the external ratio call fails.

### Proof of Concept
1. Alice deposits zstSTX (or stSTX) as collateral and borrows USDC against it via `market.borrow`, incurring debt (`mainnet/contracts/market/v0-4-market.clar` `borrow`).
2. The external contract `SP4SZE494VC2YC5JYG7AYFQ44F5Q4PYV7DVMDPBG.block-info-nakamoto-ststx-ratio-v2` becomes unavailable/reverts (deprecated, migrated, or its own admin pauses/shuts it down) — analogous to vlCVX being shut down in the referenced report.
3. Alice calls `collateral-remove` to withdraw her stSTX/zstSTX collateral, or attempts `repay`+`collateral-remove`, or a liquidator calls `liquidate`/`liquidate-redeem` on her position.
4. `collateral-remove` (line 1107) computes `notional-valued-assets` via `get-notional-evaluation`, which resolves the price of the stSTX/zstSTX asset via `resolve-callcode` → `resolve-ststx` → `call-ststx-ratio` (lines 339-341, 1015-1016).
5. `call-ststx-ratio`'s underlying `contract-call?` fails/traps; `unwrap!` in `resolve-ststx` returns `ERR-ORACLE-CALLCODE`, and the entire transaction reverts.
6. Alice's collateral and any liquidator's ability to liquidate remain permanently blocked as long as the external contract is unavailable — funds are frozen with no recovery mechanism in the current code.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L339-341)
```text
(define-private (resolve-ststx (p uint))
  (let ((ratio (unwrap! (call-ststx-ratio) ERR-ORACLE-CALLCODE)))
    (ok (mul-div-down p ratio STSTX-RATIO-DECIMALS))))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1012-1016)
```text
;; -- Oracle (public call for ststx ratio) -----------------------------------

;; ststx ratio transformation
(define-public (call-ststx-ratio)
  (contract-call? 'SP4SZE494VC2YC5JYG7AYFQ44F5Q4PYV7DVMDPBG.block-info-nakamoto-ststx-ratio-v2 get-ststx-ratio-v3))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1107-1136)
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
```
