### Title
Bad-debt socialization instantly writes down a vault's liquidity index, allowing an attacker-triggered socialization to force-liquidate other users holding that vault's zToken as collateral - ([File: mainnet/contracts/market/v0-4-market.clar])

### Summary
When a liquidation leaves a borrower with no collateral remaining, `liquidate` in `v0-4-market.clar` calls into the vault's `socialize-debt`, which immediately rewrites the vault's `lindex` (liquidity index) proportionally to the loss. That same `lindex` is the exact value `resolve-ztoken` (a market oracle "callcode" transform) uses to price the vault's zToken for every other user who holds that zToken as collateral. This mirrors the DYAD kerosene bug: a value derived from pool-level state (here, vault assets/debt) that other users' health checks depend on can be moved instantly, in one block, by an action an ordinary participant can trigger (creating/allowing a bad-debt position and then liquidating it).

### Finding Description
`socialize-debt` writes the new liquidity index as: [1](#0-0) 

```
new-lindex = current-lindex * (old-total-assets - debt-reduction) / old-total-assets
```

This `lindex` is exactly what the market's oracle callcode uses to price zTokens for every position that holds them as collateral: [2](#0-1) 

`resolve-ztoken` multiplies the underlying asset's oracle price by `cached-lindex / INDEX-PRECISION` — there is no smoothing, delay, or TWAP; whatever `lindex` is at call time is used immediately in the same transaction/block for health/liquidation calculations of any position holding that zToken.

`socialize-debt` is invoked from the market's `liquidate` function whenever a liquidation leaves the borrower with `no-collateral-left`: [3](#0-2) 

Crucially, `liquidate` is a public, unprivileged entry point callable by any `tx-sender`: [4](#0-3) 

So the flow an ordinary principal can trigger is:
1. Open (or control, e.g. via an alt account) a position with debt denominated in a vault V (e.g. `v0-vault-usdc`), sized so that after a liquidation the borrower ends up with `no-collateral-left` while still carrying scaled debt in V.
2. Call `liquidate` on that position (self-liquidate or have an accomplice liquidate it). This triggers the bad-debt-socialization branch, which calls `socialize-debt` on vault V, instantly slashing V's `lindex`.
3. Because `lindex` is read live by `resolve-ztoken`/`price-resolve` for any other transaction resolving zV's price (e.g., another user's `collateral-remove`, `borrow`, or a third party's `liquidate` call against a victim holding zV as collateral), the victim's collateral USD value used in `is-healthy` drops in the same block the attack transaction lands, with no time for the victim to react — exactly the kind of "kerosene" instantaneous value manipulation described in the report, but here it operates on the liquidity index feeding zToken pricing rather than a TVL/supply ratio. Once the victim's health check fails, the attacker (or an accomplice) can immediately call `liquidate` against the now-undercollateralized victim.

The severity difference from the original kerosene report is that triggering it costs the attacker a socialized bad debt (the vault absorbs the loss), so it is not "free" like the kerosene TVL-withdrawal trick, but it is still a deliberate, attacker-initiated, single-block manipulation of a price input shared by other users' health checks, and no TWAP/staleness protection exists for `lindex` reads by `resolve-ztoken` (the staleness checks in `price-resolve` only cover the oracle timestamp/publish-time, not `lindex`, which has no timestamp at all).

### Impact Explanation
This lands on **temporary freezing of funds / theft via forced liquidation**: victims holding the affected zToken as collateral can be pushed below the liquidation threshold purely because of the attacker's bad-debt socialization transaction, then liquidated (losing the liquidation penalty) despite not having actually taken on more risk or having any oracle price of the underlying asset move. This is analogous to the referenced medium-severity finding, and in Zest it additionally touches shared collateral valuation across all users of a given zToken vault, since `lindex` is a single global per-vault value with no TWAP.

### Likelihood Explanation
Likelihood is moderate: the attacker needs a position that (a) uses a given vault's debt, and (b) ends up fully collateral-less on liquidation while retaining scaled debt in that vault, and needs a victim who holds the corresponding zToken as collateral near the liquidation threshold at that moment. This requires deliberate setup (e.g., attacker intentionally overleveraging their own low-collateral position and self-liquidating, or waiting for organic bad debt) but does not require any privileged access, DAO compromise, or oracle-publisher collusion — only ordinary `liquidate` calls, which are explicitly in scope.

### Recommendation
- Decouple the zToken's on-chain price from the live, instantaneously-mutable `lindex` — e.g., apply a TWAP/smoothing or a maximum per-block delta cap to `lindex` changes used specifically for oracle price resolution (`resolve-ztoken`), separate from the internal accounting `lindex` used for share/asset conversion.
- Alternatively, require a minimum settlement delay (grace period) after `socialize-debt` before the updated `lindex` is used for third-party health checks, mirroring the existing liquidation grace-period mechanism (`liquidation-grace-periods`) already present in the codebase.
- Add monitoring/alerts on large single-block drops in vault `lindex` correlated with liquidation calls that immediately precede other liquidations in the same or next block.

### Proof of Concept
1. Attacker opens position P1 with tiny collateral and debt fully denominated in vault V (e.g. USDC vault), positioned so on liquidation, `coll-removed` clears all collateral (`no-collateral-left = true`) while leaving `debt-updated > 0`.
2. Attacker (or colluding party) calls `liquidate(P1, ...)`. Because P1 ends with no collateral left, the market's bad-debt path fires (`v0-4-market.clar:1534-1560`), calling `socialize-debt` on vault V (`v0-vault-usdc.clar:942-967`), instantly reducing `lindex`.
3. In the same block, attacker calls `liquidate` against victim V2, who holds `zUSDC` (vault V's receipt token) as collateral near the `LTV-LIQ-PARTIAL` threshold. `price-resolve` → `resolve-callcode` → `resolve-ztoken` (`v0-4-market.clar:343-358`) now returns a lower USD price for `zUSDC` using the freshly-slashed `lindex`, pushing V2's `current-ltv` (`v0-4-market.clar:1424-1426`) above `ltv-liq-partial`, satisfying `health-check` (`v0-4-market.clar:1435`) and allowing the attacker to liquidate V2 for a profit, despite no external asset price having moved.

### Citations

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L944-967)
```text
(define-public (socialize-debt (scaled-amount uint))
  (let ((scaled-principal (var-get principal-scaled))
        (borrowed (var-get total-borrowed))
        (idx (var-get index))
        (current-assets (var-get assets))
        (current-lindex (var-get lindex))
        (old-total-assets (total-assets))
        (debt-reduction (mul-div-down scaled-amount idx INDEX-PRECISION))
        (principal-reduction (if (> scaled-principal u0)
                                (mul-div-down scaled-amount borrowed scaled-principal)
                                u0))
        ;; Write down lindex proportionally to loss in total-assets
        (new-lindex (if (and (> old-total-assets u0) (> old-total-assets debt-reduction))
                       (mul-div-down current-lindex (- old-total-assets debt-reduction) old-total-assets)
                       u0)))

    (try! (check-caller-auth))
    (asserts! (> scaled-amount u0) ERR-AMOUNT-ZERO)

    (var-set lindex new-lindex)
    (var-set principal-scaled (if (> scaled-principal scaled-amount) (- scaled-principal scaled-amount) u0))
    (var-set total-borrowed (if (> borrowed principal-reduction) (- borrowed principal-reduction) u0))
    (var-set assets (if (> current-assets principal-reduction) (- current-assets principal-reduction) u0))

```

**File:** mainnet/contracts/market/v0-4-market.clar (L343-358)
```text
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

**File:** mainnet/contracts/market/v0-4-market.clar (L1382-1396)
```text
(define-public (liquidate
                (borrower principal)
                (collateral-ft <ft-trait>)
                (debt-ft <ft-trait>)
                (debt-amount uint)
                (min-collateral-expected uint)
                (collateral-receiver (optional principal))
                (price-feeds (optional (list 3 (buff 8192)))))
  (let (
    (feeds-check (try! (write-feeds price-feeds)))
    (liquidator contract-caller)
    (position (try! (get-liquidation-position borrower)))
    (pos-full (try! (get-full-position borrower)))
    (mask (get mask position))
    (group (try! (get-egroup mask)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1534-1560)
```text
      ;; Handle bad debt socialization if no collateral left
      (let ((bad-debt-socialized 
              (if no-collateral-left
                  (let ((stripped-debt-list (filter-out-debt-asset (get debt pos-full) debt-aid))
                        (fresh-debt-list (if (is-eq debt-updated u0)
                                             stripped-debt-list
                                             (unwrap-panic (as-max-len?
                                               (append stripped-debt-list
                                                       { aid: debt-aid, scaled: debt-updated })
                                               u64)))))
                    (if (> (len fresh-debt-list) u0) ;; if still has debt
                      (let ((socialization-result (fold socialize-debt-asset 
                                                        fresh-debt-list 
                                                        { borrower: borrower, success: true })))
                        (asserts! (get success socialization-result) ERR-BAD-DEBT-SOCIALIZATION-FAILED)
                        ;; emit bad-debt-socialized event
                        (print {
                          action: "bad-debt-socialized",
                          caller: contract-caller,
                          data: {
                            borrower: borrower,
                            debt-list: fresh-debt-list
                          }
                        })
                        true)
                      false))
                  false)))
```
