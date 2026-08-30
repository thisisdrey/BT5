## Title
Share Price Manipulation via `socialize-debt`-Driven Total-Assets Deflation Allows Disproportionate VRT (zft) Minting and Theft From Existing Depositors - ([File: mainnet/contracts/vault/v0-vault-usdc.clar])

### Summary
The Zest vault contracts (`v0-vault-usdc.clar`, `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`, `v0-vault-stx.clar`, `v0-vault-usdh.clar`) mint tokenized shares (`zft`) using a pro-rata formula that divides by `total-assets-preview`. Bad-debt socialization (the analog of "slashing" in the external report) can legitimately drive `total-assets` down to a very small value while `total-supply` (outstanding `zft`) remains large, without ever hitting the guarded `is-eq ta u0` branch. In that state a subsequent depositor is minted a share count disproportionate to their contribution, diluting existing VRT holders and enabling extraction of their underlying balance on redeem.

### Finding Description
Share minting is computed in `convert-to-shares-preview`: [1](#0-0) 

This only special-cases the exact-zero conditions (`ts == 0` or `ta == 0`); it does not protect against `ta` being a very small nonzero value relative to `ts`. `total-assets-preview` is derived from the `assets` variable and unpaid interest: [2](#0-1) 

`assets` is reduced directly by `socialize-debt`, which is the vault-side handler invoked whenever the market fully writes off a borrower's bad debt (the market analog of "slashing"): [3](#0-2) 

This is reached from an ordinary principal's `liquidate`/`liquidate-multi` call in the market contract whenever a borrower's collateral fully depletes, triggering `socialize-debt-asset` and `vault-socialize-debt`: [4](#0-3) 

Because `socialize-debt` can be invoked repeatedly (once per bad-debt position, and across multiple liquidation events), an attacker who observes (or engineers, e.g. via price-oracle-driven liquidations of thin positions) enough socialization events can push `assets` (hence `total-assets-preview`) down to a value that is nonzero but tiny, while `total-supply` of `zft` stays large. At that point:

`shares_minted = amount * ts / ta`

becomes disproportionately large for any given `amount`, because `ta` is near-zero relative to `ts`. The attacker deposits a comparatively small `amount`, is minted a share count that represents a majority (or large fraction) of the post-deposit `zft` supply, and can then redeem to claim underlying tokens that were contributed by other, pre-existing depositors — the exact "unfair dilution" failure mode described in the external report, just triggered through the near-zero rather than the exactly-zero branch, and the exploiting party being a subsequent depositor rather than the vault's original users.

Additionally, unlike `redeem`, which asserts a nonzero output (`ERR-OUTPUT-ZERO`), `deposit` has no equivalent `(> inkind u0)` check: [5](#0-4) 

so in the exact `ta == 0` edge case a depositor can also silently donate their entire principal (minted 0 shares) to existing `zft` holders — the mirror image of the report's dilution scenario, still a share-math fairness defect in the same code path.

### Impact Explanation
This is a direct theft-of-user-funds vector: an attacker can, by depositing at the moment `total-assets` is deflated relative to `total-supply`, mint shares that entitle them to redeem a disproportionate amount of the underlying tokens contributed by other depositors. This falls under the Critical impact class ("direct theft of user funds at rest ... or protocol insolvency") because it directly transfers principal value from existing depositors to the attacker through legitimate-looking deposit/redeem calls, and repeated socialization events compound the deflation, worsening the disproportion over time.

### Likelihood Explanation
Reaching the required state does not require any privileged action or DAO compromise — `socialize-debt` is triggered purely by ordinary `liquidate`/`liquidate-multi` calls on undercollateralized positions with exhausted collateral, a designed and expected market mechanism. An attacker with capital (or using their own thin borrow positions to create sacrificial bad debt) can deliberately manufacture multiple socialization events on a given vault asset to deflate `total-assets` relative to `total-supply`, then deposit to mint outsized shares. This requires capital and multiple transactions but no special privilege, making it a realistic, self-triggerable attack path.

### Recommendation
Guard `convert-to-shares-preview` (and its `convert-to-assets-preview` counterpart) against not just exact-zero but also disproportionately-small `total-assets-preview` relative to `total-supply-preview` — e.g., require a minimum share price or revert/pause deposits when the assets-to-supply ratio falls below a safe threshold following a socialization event, mirroring the external report's own remediation intent of preventing minting logic from operating correctly only in the boundary `== 0` case. Also add the missing `(asserts! (> inkind u0) ERR-OUTPUT-ZERO)` check to `deposit` for symmetry with `redeem`.

### Proof of Concept
1. Attacker (or colluding party) opens borrow positions against volatile/thin collateral in one or more Zest vaults (e.g., `v0-vault-usdc`).
2. Attacker lets/forces these positions become undercollateralized (e.g., via natural price moves) and calls `liquidate`/`liquidate-multi` on the market contract until `no-collateral-left` is true, which invokes `socialize-debt-asset` → `vault-socialize-debt` → the vault's `socialize-debt`, repeatedly driving `assets` toward a very small nonzero value while `zft` `total-supply` remains large.
3. Attacker calls `deposit` with a modest `amount`. `convert-to-shares-preview` computes `shares = amount * ts / ta` with `ta` near-zero, minting the attacker a disproportionately large fraction of `zft` relative to their contribution.
4. Attacker calls `redeem`, and via `convert-to-assets-preview` receives underlying tokens far exceeding their deposited `amount`, funded by the balance contributed by pre-existing depositors — net theft of other users' principal.

### Citations

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L306-313)
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

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L761-782)
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
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L942-968)
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

    (print {
      action: "socialize-debt",
      caller: contract-caller,
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
