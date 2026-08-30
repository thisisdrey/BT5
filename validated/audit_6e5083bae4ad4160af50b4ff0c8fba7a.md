### Title
`redeem` reverts on rounding-to-zero share amounts, blocking `liquidate-redeem` / `collateral-remove-redeem` on small positions - ([File: mainnet/contracts/vault/v0-vault-usdc.clar])

### Summary
The vault `redeem` function (present in all `v0-vault-*.clar` contracts, e.g. `v0-vault-usdc.clar`, `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`, `v0-vault-usdh.clar`, `v0-vault-stx.clar`) hard-reverts with `ERR-OUTPUT-ZERO` whenever the share→asset conversion rounds down to zero, even though the caller supplied a non-zero share amount. This is the same root-cause pattern as the referenced report: a redemption call reverting on a non-zero but "small" input because of rounding in the preview math, rather than silently skipping or capping the operation.

### Finding Description
`convert-to-assets-preview` computes `inkind` via `mul-div-down amount ta ts` [1](#0-0) . In `redeem`, this `inkind` value is asserted to be strictly greater than zero, and the whole call reverts otherwise: [2](#0-1) 

This function is called downstream from two market entry points that a liquidator or an ordinary user can trigger with attacker-influenceable amounts:

- `liquidate-redeem` computes `collateral-seized` from the liquidation math and passes it directly into `vault-redeem`, which forwards to the vault's `redeem`: [3](#0-2) 
- `collateral-remove-redeem` similarly removes zToken collateral and then calls `vault-redeem` with the same `amount`: [4](#0-3) 

Whenever the vault's `ta/ts` ratio (total-assets-preview / total-supply-preview) is such that `mul-div-down(shares, ta, ts) == 0` for the specific `shares` amount involved — which happens for any `shares < ts/ta` roughly, i.e. any share amount below the "one wei of underlying" threshold for that vault's current exchange rate — the entire `liquidate-redeem` or `collateral-remove-redeem` transaction reverts with `ERR-OUTPUT-ZERO`, even though the upstream `liquidate` function already validated `coll-final > u0` (`ERR-ZERO-LIQUIDATION-AMOUNTS`) [5](#0-4) . This mirrors exactly the report's conclusion: the check-for-zero-output pattern only catches balance==0/1 in the trivial case, but at any exchange rate skew a non-trivial, non-zero share amount can still round to zero assets and unexpectedly abort the transaction.

### Impact Explanation
This causes a **temporary freezing of funds/functionality** for the specific compound operations (`liquidate-redeem`, `collateral-remove-redeem`) whenever the seized/removed zToken amount is small relative to the vault's current share price. A liquidator attempting to atomically liquidate-and-redeem a small dust collateral position, or a user attempting to atomically remove-and-redeem a small ztoken collateral amount, will have their transaction revert, even though the position itself is a legitimate liquidation/removal target. Since `liquidate` (without redeem) and `collateral-remove` (without redeem) do not perform this share→asset conversion and thus do not have this check, a manual two-step workaround exists, which limits the severity — the underlying collateral itself is not permanently frozen, only the convenience atomic path is blocked.

### Likelihood Explanation
Likelihood is moderate: it requires a vault with a share price sufficiently above 1:1 (accrued interest/treasury LP minting increases `ta` relative to `ts` over time) combined with a small enough collateral/z-share amount being liquidated or removed. As vaults accrue more yield over their lifetime, the "dust" threshold that triggers this revert grows (as the original report notes, this can reach non-trivial magnitudes, not just balance==1).

### Recommendation
In `redeem` (all `v0-vault-*.clar` contracts) and in the callers (`liquidate-redeem`, `collateral-remove-redeem`), avoid hard-reverting the entire compound transaction when the assets-preview rounds to zero for a non-zero share amount. Options: (a) explicitly pre-check `convert-to-assets-preview` before attempting the redeem step and skip/short-circuit the redeem leg gracefully (treating it as "nothing to redeem, keep zTokens") rather than aborting the whole liquidation/removal, or (b) round up in `convert-to-assets-preview` for redeem paths reached from liquidation contexts so a non-zero share amount never yields a hard revert.

### Proof of Concept
1. Let a vault (e.g. `v0-vault-usdc`) have accrued enough interest/treasury LP minting such that `total-assets-preview() / total-supply-preview() > 1` (share price > 1 underlying unit per share).
2. A borrower's position has a small residual zToken (`zUSDC`) collateral balance `C` such that `mul-div-down(C, ta, ts) == 0` while `C > 0`.
3. A liquidator calls `liquidate-redeem` on this borrower; `liquidate` succeeds and returns `collateral-seized = C > 0` (passes `ERR-ZERO-LIQUIDATION-AMOUNTS` check).
4. `vault-redeem(underlying-id, C, min-underlying, receiver)` is invoked, which calls the vault's `redeem`, where `inkind = convert-to-assets-preview(C) == 0`.
5. The `asserts! (> inkind u0) ERR-OUTPUT-ZERO` fires, reverting the entire `liquidate-redeem` transaction, forcing the liquidator to fall back to plain `liquidate` (retaining the zTokens instead of underlying).

### Citations

**File:** mainnet/contracts/vault/v0-vault-ststxbtc.clar (L317-328)
```text
(define-private (convert-to-assets-preview (amount uint))
  (let ((ta (total-assets-preview))
        (ts (total-supply-preview)))
    (if (is-eq ta u0)
        u0
        (if (is-eq ts u0)
            u0
            (mul-div-down amount ta ts)))))

;; -- Debt helpers -----------------------------------------------------------

(define-private (total-debt)
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L799-817)
```text
    (account contract-caller)
    (current-assets (var-get assets))
    (balance (get-balance-internal account))
    (balance-check (asserts! (>= balance amount) ERR-INSUFFICIENT-BALANCE))
    (available-assets (get-available-assets))
    (inkind (convert-to-assets-preview amount)))

  (asserts! (>= current-assets inkind) ERR-INSUFFICIENT-ASSETS)
  (asserts! (not (get redeem states)) ERR-PAUSED)
  (asserts! (> amount u0) ERR-AMOUNT-ZERO)
  (asserts! (> inkind u0) ERR-OUTPUT-ZERO)
  (asserts! (>= inkind min-out) ERR-SLIPPAGE)
  (asserts! (>= available-assets inkind) ERR-INSUFFICIENT-LIQUIDITY)

  (try! (ft-burn? zft amount account))
  (try! (send-underlying inkind recipient))
  (var-set assets (- current-assets inkind))

  (print {
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1234-1257)
```text
    (vault-redeem underlying-id amount min-underlying funds-receiver)))

;; -- Debt operations --------------------------------------------------------

(define-public (borrow (ft <ft-trait>) (amount uint) (receiver (optional principal)) (price-feeds (optional (list 3 (buff 8192)))))
  (let ((address (contract-of ft))
        (asset (try! (get-asset address)))
        (asset-id (get id asset))
        (account contract-caller)
        (funds-receiver (match receiver recv recv contract-caller))
        (feeds-check (try! (write-feeds price-feeds)))
        
        ;; Step 1: Get position WITHOUT resolving prices
        (position (try! (get-position account)))
        (mask (get mask position))
        
        ;; Step 2: Accrue user's positions (populates cache for ztokens)
        (u-debt (accrue-user-debts (get debt position)))
        (u-coll (accrue-user-collateral (get collateral position)))
        
        ;; Step 3: Accrue the asset being borrowed (needed for index access)
        (unused (accrue-and-cache asset-id))
        
        ;; Step 4: NOW safe to resolve prices (cache is populated)
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1488-1493)
```text
    (asserts! (not (is-liquidation-paused debt-aid)) ERR-LIQUIDATION-PAUSED)
    (asserts! (is-eq contract-caller tx-sender) ERR-AUTHORIZATION)
    (asserts! (> debt-amount u0) ERR-AMOUNT-ZERO)
    (asserts! (> debt-to-repay u0) ERR-ZERO-LIQUIDATION-AMOUNTS)
    (asserts! (> coll-final u0) ERR-ZERO-LIQUIDATION-AMOUNTS)
    (asserts! (>= coll-final min-collateral-expected) ERR-SLIPPAGE)
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1629-1645)
```text
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
