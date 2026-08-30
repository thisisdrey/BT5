No vulnerability found for this question.

`is-liquidation-paused` is a pure state read: it only inspects `var-get pause-liquidation`, `map-get? liquidation-grace-periods GLOBAL-LIQUIDATION-GRACE-ID`, `map-get? liquidation-grace-periods asset-id`, and `stacks-block-time`, none of which are influenced by the `receiver`, `price-feeds`, `debt-amount`, or any other caller-supplied argument to `liquidate-redeem`. [1](#0-0) 

Tracing the call path: `liquidate-redeem` passes `borrower`, `collateral-ft`, `debt-ft`, `debt-amount`, `min-collateral-expected`, and `price-feeds` into `liquidate`, and separately resolves `funds-receiver` for the later `vault-redeem` call — the receiver is never threaded into the pause/grace check at all. [2](#0-1) 

Since the pause/grace state (`pause-liquidation` var, `liquidation-grace-periods` map, `stacks-block-time`) is set only by privileged/DAO paths and block progression, an unprivileged caller varying the redemption receiver across two calls within a Clarinet simnet test would produce identical `is-liquidation-paused` results for a given `asset-id` at a fixed block time — there is no mechanism by which changing the receiver could cause divergence. The premise that this converts "a rounding direction into a repeatable extraction" has no code-level basis; no rounding operation feeds into this function, and no caller-controlled input reaches it.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L691-697)
```text
(define-private (is-liquidation-paused (asset-id uint))
  (let ((manual-pause (var-get pause-liquidation))
        (global-grace-end (default-to u0 (map-get? liquidation-grace-periods GLOBAL-LIQUIDATION-GRACE-ID)))
        (asset-grace-end (default-to u0 (map-get? liquidation-grace-periods asset-id)))
        (global-grace-active (< stacks-block-time global-grace-end))
        (asset-grace-active (< stacks-block-time asset-grace-end)))
    (or manual-pause global-grace-active asset-grace-active)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1604-1645)
```text
(define-public (liquidate-redeem
                (borrower principal)
                (collateral-ft <ft-trait>)
                (debt-ft <ft-trait>)
                (debt-amount uint)
                (min-collateral-expected uint)
                (min-underlying uint)
                (receiver (optional principal))
                (price-feeds (optional (list 3 (buff 8192)))))
  (let ((coll-address (contract-of collateral-ft))
        (coll-asset (try! (get-asset coll-address)))
        (ztoken-id (get id coll-asset))
        ;; Map zToken to underlying vault ID for redemption
        (underlying-id (if (is-eq ztoken-id zSTX) STX
                       (if (is-eq ztoken-id zsBTC) sBTC
                       (if (is-eq ztoken-id zstSTX) stSTX
                       (if (is-eq ztoken-id zUSDC) USDC
                       (if (is-eq ztoken-id zUSDH) USDH
                       (if (is-eq ztoken-id zstSTXbtc) stSTXbtc
                       u100)))))))  ;; invalid sentinel for non-ztoken
        (funds-receiver (match receiver recv recv contract-caller)))
    
    ;; Validate collateral is a zToken
    (asserts! (is-ztoken ztoken-id) ERR-UNKNOWN-VAULT)
    
    ;; Step 1: Liquidate with market as receiver (market receives zTokens)
    (let ((liq-result (try! (liquidate borrower
                                       collateral-ft
                                       debt-ft
                                       debt-amount
                                       min-collateral-expected
                                       (some current-contract)  ;; zTokens go to market
                                       price-feeds)))
          (collateral-seized (get collateral liq-result))
          (debt-repaid (get debt liq-result)))
      
      ;; Step 2: Redeem zTokens for underlying
      ;; Market now holds zTokens, vault-redeem burns them and sends underlying to receiver
      (let ((underlying-amount (try! (vault-redeem underlying-id 
                                                   collateral-seized 
                                                   min-underlying 
                                                   funds-receiver))))
```
