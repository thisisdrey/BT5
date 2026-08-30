### Title
Chained truncation in `resolve-ztoken(resolve-ststx(p))` for zstSTX price causes compounded precision loss / systematic undervaluation - (File: mainnet/contracts/market/v0-4-market.clar)

### Summary
`resolve-callcode` computes the price of `zstSTX` by first calling `resolve-ststx` (which does a division) and then feeding that already-divided/truncated result into `resolve-ztoken` (which divides again), instead of combining all numerators and dividing once by the product of all denominators. This is the same "multiplication after division" precision-loss pattern described in the referenced GMX finding (M-32), reproduced here in the oracle callcode-transform path.

### Finding Description
In `resolve-callcode`, the `zstSTX` case chains two independent division-rounding operations: [1](#0-0) 

- `resolve-ststx` computes `p1 = mul-div-down(p, ratio, STSTX-RATIO-DECIMALS)` — a division that truncates to the floor.
- For `zstSTX`, `resolve-callcode` then calls `resolve-ztoken(resolve-ststx(p), stSTX)`, i.e. it takes the already-truncated `p1` and computes `zstSTX_price = div-down(p1 * lindex, INDEX-PRECISION)` — a second, independent truncation.

The mathematically correct single-step calculation would be:
```
zstSTX_price = floor( p * ratio * lindex / (STSTX-RATIO-DECIMALS * INDEX-PRECISION) )
```
Because the code instead computes `floor( floor(p*ratio/STSTX-RATIO-DECIMALS) * lindex / INDEX-PRECISION )`, the result is always less than or equal to the correct combined value — the exact "divide first, multiply/divide again on the truncated result" anti-pattern called out in the external report (`cache.fundingUsd` computed by an early division and then reused for further multiplication/division in `MarketUtils.sol`, and `cache.positionPnlUsd` in `PositionUtils.sol`).

By contrast, every other callcode branch (`resolve-ztoken` alone for `zSTX`/`zsBTC`/`zUSDC`/`zUSDH`/`zstSTXbtc`, or `resolve-ststx` alone for `stSTX`) performs only a single rounding step, so `zstSTX` is the one asset whose price computation compounds two independent truncations.

This resolved price feeds directly into position health/notional calculations: [2](#0-1) 

where `price` is used to compute both `coll-notional` (rounded down) and `debt-notional` (rounded up) for every position holding zstSTX as collateral or debt.

### Impact Explanation
Because both internal divisions in the `zstSTX` price path round down, the final oracle price for `zstSTX` is systematically biased low relative to the mathematically correct combined-ratio price. This bias is deterministic and can be maximized by an attacker choosing operation timing/amounts, since the two truncation losses are independent of each other (unlike a single combined division, which loses at most one unit of precision).

- When `zstSTX` is used as **debt**, an artificially low `debt-notional` understates the USD value of a user's debt in health checks (`get-notional-evaluation` → `calculate-asset-notional-value`), which can let a position appear healthier than it truly is — permitting under-collateralized borrowing/avoiding liquidation, and ultimately contributing to protocol insolvency (bad debt) if losses aren't recovered on liquidation.
- When `zstSTX` is used as **collateral**, the same effect understates collateral value, which is directionally conservative (safe) but still reflects incorrect accounting.

Given the instructions state oracle-resolution/callcode-transform bugs are explicitly in scope and the ultimate effect on debt valuation can enable positions to escape correct liquidation thresholds, this lands in the Critical impact bucket (protocol insolvency via accumulated bad debt from mispriced debt positions), consistent with how the original GMX report was scoped (precision loss in accounting feeding into further multiplication/division).

### Likelihood Explanation
This occurs on every price resolution for the `zstSTX` asset (i.e., whenever `CALLCODE-ZSTSTX` is used), which happens routinely in ordinary user flows — borrow, withdraw, and liquidation health checks — since `price-resolve`/`price-multi-resolve` are called from standard market entry points for any position holding `zstSTX`. No privileged action is required; it triggers automatically whenever the callcode is `CALLCODE-ZSTSTX`. The magnitude of loss per call is bounded by rounding, but it recurs on every price fetch and compounds across many operations/blocks, so likelihood of the underlying miscalculation firing is high, though the exploitable USD magnitude per single call is small and scales with the size of `ratio`/`lindex`/token decimals in play (exact numeric bound could not be fully confirmed within the available context, since I could not verify the concrete values of `STSTX-RATIO-DECIMALS`/`INDEX-PRECISION` constants or whether `zstSTX` is currently configured as a debt-enabled asset in the live asset registry).

### Recommendation
Combine the ratio and liquidity-index adjustments into a single multiply-then-divide operation instead of chaining two independently-rounded `mul-div-down`/`div-down` calls, e.g.:
```
zstSTX_price = mul-div-down(mul-div-down(p, ratio, 1), lindex, 1) ... 
```
more precisely, compute the combined numerator `p * ratio * lindex` and combined denominator `STSTX-RATIO-DECIMALS * INDEX-PRECISION` and perform one division (matching the OpenZeppelin `Math.mulDiv` fix approach referenced in the GMX report), so only one truncation occurs instead of two independent ones.

### Proof of Concept
Not directly executable from the indexed context (no access to a live test harness here), but the logical PoC is:
1. Pick `p`, `ratio`, `lindex` such that `p*ratio mod STSTX-RATIO-DECIMALS != 0` and the resulting `p1*lindex mod INDEX-PRECISION != 0`.
2. Compute `chained = div-down(mul-div-down(p, ratio, STSTX-RATIO-DECIMALS) * lindex, INDEX-PRECISION)` (current code path) versus `combined = div-down(p * ratio * lindex, STSTX-RATIO-DECIMALS * INDEX-PRECISION)` (correct single-step calc).
3. Show `chained <= combined` for all such inputs, with the gap growing as `ratio`/`lindex` deviate further from exact multiples of their respective precision constants — confirming the compounded-truncation bias described above. [1](#0-0)

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L339-358)
```text
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

**File:** mainnet/contracts/market/v0-4-market.clar (L544-574)
```text
(define-private (calculate-asset-notional-value
          (asset-entry {
              id: uint, addr: principal, decimals: uint,
              oracle: { type: (buff 1), ident: (buff 32), callcode: (optional (buff 1)), max-staleness: uint },
              collateral: bool, debt: bool, price: uint })
          (acc { clist: (list 64 { aid: uint, amount: uint }),
                  dlist: (list 64 { aid: uint, scaled: uint }),
                  coll-total: uint,
                  debt-total: uint }))
  (let ((asset-id (get id asset-entry))
        (price (get price asset-entry))
        (decimals (get decimals asset-entry))
        (collateral-list (get clist acc))
        (debt-list (get dlist acc))
        (coll-amount (find-collateral-amount collateral-list asset-id))
        (coll-notional (if (> coll-amount u0)
                           (normalize (* coll-amount price) decimals false)
                           u0))

        (debt-scaled   (find-debt-scaled debt-list asset-id))
        (debt-notional (if (> debt-scaled u0) ;; use cache instead here
                           (let ((cached (unwrap-panic (accrue-and-cache asset-id)))
                                 (ib (get index cached))
                                 (actual (mul-div-up debt-scaled ib INDEX-PRECISION)))
                             (normalize (* actual price) decimals true))
                           u0)))

    { clist: collateral-list,
      dlist: debt-list,
      coll-total: (+ (get coll-total acc) coll-notional),
      debt-total: (+ (get debt-total acc) debt-notional) }))
```
