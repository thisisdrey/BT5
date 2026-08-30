[1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L1211-1224)
```text
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
```
