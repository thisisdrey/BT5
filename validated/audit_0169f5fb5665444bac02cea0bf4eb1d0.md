[1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L668-676)
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
```

**File:** mainnet/contracts/market/v0-4-market.clar (L678-687)
```text
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

**File:** mainnet/contracts/market/v0-4-market.clar (L1258-1267)
```text
        (assets (get-assets mask))

        ;; Calculate current health with current mask
        (current-group (try! (get-egroup mask)))
        (current-ltvb (buff-to-uint-be (get LTV-BORROW current-group)))

        ;; LTV
        (notional-valued-assets (get-notional-evaluation { position: position, assets: assets }))
        (collateral-value (get collateral notional-valued-assets))
        (debt-value (get debt notional-valued-assets)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1281-1282)
```text
          (debt-increase (try! (get-asset-value asset amount true)))
          (debt-post-increased (+ debt-value debt-increase)))
```
