## Analysis

The reported `TimeBasedCollateralPool` bug is a **share/unit accounting divergence**: an external process (`claimant` role) can reduce the pool's real token balance without burning the corresponding "units" that represent depositor claims, permanently decoupling the unit-to-token ratio and eventually corrupting deposit/withdraw math.

Zest v2's tokenized vaults (`v0-vault-stx.clar`, `v0-vault-usdc.clar`, `v0-vault-usdh.clar`, `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`) implement the exact same class of share-based accounting, and contain the analogous decoupling mechanism plus a missing zero-output guard.

### Title
Zero-share deposit due to socialize-debt driving total-assets to zero while zft total-supply remains non-zero - (File: `mainnet/contracts/vault/v0-vault-usdc.clar` and equivalent `v0-vault-*.clar` files)

### Summary
`socialize-debt` writes down a vault's internal `assets` variable (and `total-borrowed`/`principal-scaled`) to absorb bad debt from a liquidation with no remaining collateral, but never burns any `zft` shares. This is the same structural flaw as the reported bug: an externally-triggerable value-reduction event decouples "shares" from "assets" without any corresponding unit adjustment. Combined with the fact that `deposit` never validates that the shares it mints (`inkind`) are non-zero, a depositor calling `deposit` after `total-assets` has been driven to `u0` transfers real underlying tokens into the vault but receives `u0` `zft` shares in return — a permanent, unrecoverable loss of their deposit.

### Finding Description
`socialize-debt` in each vault writes down `assets` to zero when the socialized debt fully consumes the reserve, but never adjusts `zft` total-supply: [1](#0-0) 

`total-assets`/`total-assets-preview` derive directly from this `assets` variable: [2](#0-1) 

`convert-to-shares-preview` explicitly special-cases `ta == u0` (total-assets is zero) by returning `u0` shares for any deposit amount, rather than reverting: [3](#0-2) 

`deposit` computes `inkind` from this preview, checks `amount > 0` and `inkind >= min-out`, but has **no assertion that `inkind > 0`** (unlike `redeem`, which does assert `inkind > 0` via `ERR-OUTPUT-ZERO`). If the caller's `min-out` is `0` (a common default, or the value returned by a stale/pre-liquidation preview call), the deposit proceeds, real tokens are pulled in via `receive-underlying`, and `u0` shares are minted: [4](#0-3) 

Reachability: `socialize-debt` is gated by `check-caller-auth`, which is satisfied when called from `market.clar`'s bad-debt path, itself reachable by any ordinary user through the public `liquidate` flow when a position ends up with no collateral left: [5](#0-4) [6](#0-5) 

This is a fully unprivileged path: any account can trigger a liquidation that fully socializes a vault's tracked debt, driving `assets` (and therefore `total-assets`) to `u0` while `zft` total-supply remains positive from pre-existing suppliers.

### Impact Explanation
Once `total-assets` is `u0`, any subsequent `deposit` call (with `min-out` left at `0`, which is the natural default for many integrators/UIs) silently mints zero shares while consuming the depositor's real underlying tokens — a direct, permanent loss of the depositor's funds with no way to recover them (there is no accounting entry giving them a claim on the deposited amount). This satisfies the Critical impact bar: "direct theft of user funds at rest ... or permanent freezing of funds."

### Likelihood Explanation
This requires: (1) a vault whose entire tracked debt is written off via socialization (a scenario the protocol's own liquidation/bad-debt-socialization logic supports and is reachable by any liquidator without special privileges), and (2) a subsequent depositor who does not set a protective non-zero `min-out`. Both conditions are realistic — bad-debt socialization is a normal (if rare) operational event, and many deposit flows default `min-out` to `0` or to a value derived from a stale preview that can be invalidated by the same-block/prior socialization event.

### Recommendation
Add an explicit `(asserts! (> inkind u0) ERR-OUTPUT-ZERO)` check to every vault's `deposit` function, mirroring the check already present in `redeem`, so that a deposit which would mint zero shares reverts instead of silently consuming the user's funds. Additionally, consider whether `socialize-debt` should also burn/adjust `zft` supply (or otherwise prevent `total-assets` from reaching exactly `u0` while `total-supply` is non-zero) to avoid this share/asset decoupling in the first place.

### Proof of Concept
1. A borrower's position becomes fully undercollateralized in, e.g., the USDC vault, with no collateral left.
2. Any account calls `market.clar`'s `liquidate`, which triggers `socialize-debt-asset` → the vault's `socialize-debt`, reducing `principal-scaled`/`total-borrowed`/`assets` such that `assets` becomes `u0` and outstanding debt is fully written off (per `mainnet/contracts/vault/v0-vault-usdc.clar:942-964`).
3. `total-assets`/`total-assets-preview` now return `u0` (`mainnet/contracts/vault/v0-vault-usdc.clar:332-344`), while `zft` total-supply from prior suppliers remains non-zero.
4. A user calls `deposit(amount, min-out=0, recipient)` with real `amount` of USDC. `convert-to-shares-preview` returns `u0` (`mainnet/contracts/vault/v0-vault-stx.clar:308-316`), the `min-out` check passes trivially (`u0 >= u0`), and `deposit` completes: the user's USDC is pulled in via `receive-underlying`, `ft-mint? zft u0 recipient` mints nothing, and `assets` is set to `amount` (`mainnet/contracts/vault/v0-vault-ststx.clar:763-796`).
5. The user has permanently lost their deposited USDC with zero `zft` shares and no recourse.

### Citations

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L332-344)
```text
(define-private (total-assets)
  (let ((current-assets (var-get assets))
        (debt (total-debt))
        (borrowed (var-get total-borrowed))
        (interest (if (> debt borrowed) (- debt borrowed) u0)))
    (+ current-assets interest)))

(define-private (total-assets-preview)
  (let ((current-assets (var-get assets))
        (debt (debt-preview))
        (borrowed (var-get total-borrowed))
        (interest (if (> debt borrowed) (- debt borrowed) u0)))
    (+ current-assets interest)))
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L942-964)
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

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L308-316)
```text
(define-private (convert-to-shares-preview (amount uint))
  (let ((ta (total-assets-preview))
        (ts (total-supply-preview)))
    (if (is-eq ts u0)
        amount
        (if (is-eq ta u0)
            u0
            (mul-div-down amount ts ta)))))

```

**File:** mainnet/contracts/vault/v0-vault-ststx.clar (L763-796)
```text
(define-public (deposit (amount uint) (min-out uint) (recipient principal))
    (let (
      (states (var-get pause-states))
      (u (try! (accrue)))
      (account contract-caller)
      (CAP-SUPPLY (var-get cap-supply))
      (current-assets (var-get assets))
      (inkind (convert-to-shares-preview amount)))

    (asserts! (not (get deposit states)) ERR-PAUSED)
    (asserts! (var-get initialized) ERR-INIT)
    (asserts! (not (var-get in-flashloan)) ERR-REENTRANCY)
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)
    (asserts! (>= inkind min-out) ERR-SLIPPAGE)
    (asserts! (<= (+ current-assets amount) CAP-SUPPLY) ERR-SUPPLY-CAP-EXCEEDED)

    (try! (receive-underlying amount account))
    (try! (ft-mint? zft inkind recipient))
    (var-set assets (+ current-assets amount))

    (print {
      action: "deposit",
      caller: contract-caller,
      data: {
        depositor: account,
        recipient: recipient,
        amount: amount,
        shares-minted: inkind,
        assets: (+ current-assets amount)
      }
    })

    (ok inkind)))

```

**File:** local-testing/contracts/market/market.clar (L901-925)
```text
(define-private (socialize-debt-asset
                (debt-entry { aid: uint, scaled: uint })
                (acc { borrower: principal, success: bool }))
  ;; Early return if previous socialization failed
  (if (not (get success acc))
      acc
      (let ((borrower (get borrower acc))
            (failed-status { borrower: borrower, success: false })
            (asset-id (get aid debt-entry))
            (scaled-debt (get scaled debt-entry)))

            ;; Socialize in vault - pass scaled directly to avoid rounding
            (unwrap! (vault-socialize-debt asset-id scaled-debt) failed-status)
            ;; Refresh cache with new indexes post-write-down (lindex decreased)
            (map-set index-cache
                     { timestamp: stacks-block-time, aid: asset-id }
                     (unwrap! (vault-accrue asset-id) failed-status))
            ;; Remove from obligation
            (unwrap! (contract-call? .market-vault
                                      debt-remove-scaled
                                      borrower
                                      scaled-debt
                                      asset-id) failed-status)
          acc)
        ))
```

**File:** local-testing/contracts/market/market.clar (L1557-1583)
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
