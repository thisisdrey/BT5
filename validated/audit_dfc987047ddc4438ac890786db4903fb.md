### Title
Depositors can front-run bad-debt `liquidate` transactions to redeem their zToken shares before `socialize-debt` write-down, shifting losses onto remaining vault depositors - (File: `mainnet/contracts/market/v0-4-market.clar`, `mainnet/contracts/vault/v0-vault-usdc.clar` and sibling vault contracts)

### Summary
Zest's per-asset vaults (`v0-vault-usdc.clar`, `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-usdh.clar`, `v0-vault-ststxbtc.clar`) pool depositor funds and socialize any bad debt created during liquidation across all zToken (share) holders by writing down the shared liquidity index (`lindex`). Because the `liquidate` transaction that triggers this write-down is publicly visible in the mempool before it is confirmed, and `redeem()` lets any zToken holder exit at the pre-write-down exchange rate, a depositor can front-run the bad-debt-triggering liquidation with a higher-fee `redeem()` call to exit before the loss is applied. The same absolute loss is then divided over a smaller remaining pool of assets, increasing the loss borne by remaining depositors — this is a direct structural analog of the Hubble Protocol `InsuranceFund` front-running-withdrawal finding (M-11).

### Finding Description
When `liquidate()` in `v0-4-market.clar` determines a borrower has no collateral left but still owes debt, it calls `socialize-debt-asset`, which in turn calls the vault's `socialize-debt` function: [1](#0-0) 

Inside the vault (e.g. `v0-vault-usdc.clar`), `socialize-debt` reduces `total-borrowed`/`principal-scaled` by the bad-debt amount and writes down `lindex` proportionally to the *current* `total-assets` at the moment the transaction executes: [2](#0-1) 

```
(new-lindex (if (and (> old-total-assets u0) (> old-total-assets debt-reduction))
               (mul-div-down current-lindex (- old-total-assets debt-reduction) old-total-assets)
               u0))
```

`old-total-assets` is read from `(total-assets)` at execution time — not at the time the liquidation was submitted to the mempool. Meanwhile, `redeem()` in the same vault contract lets any depositor burn their zTokens for underlying assets at the current (pre-write-down) exchange rate with no delay, lock-up, or queueing mechanism: [3](#0-2) 

Because Stacks transaction ordering within a block/microblock is influenced by fee (higher-fee transactions are prioritized), an attacker who observes a pending `liquidate()` call that will trigger `socialize-debt-asset` (bad debt) can submit a `redeem()` (or `collateral-remove-redeem`) with a higher fee to withdraw their shares before the liquidation/socialization executes. This removes their assets from `total-assets` beforehand, so when `socialize-debt` computes `new-lindex` using the now-smaller `old-total-assets`, the fixed `debt-reduction` amount produces a *larger* proportional write-down applied to the remaining depositors' shares (`lindex` scales `total-assets` for all zToken holders via `total-assets`/`total-assets-preview`, which is why the loss lands on whoever still holds shares when the transaction lands).

This exactly mirrors the referenced Hubble `InsuranceFund` bug: `withdraw()`/`redeem()` is a single-step, uncontrolled exit that lets a share holder dodge an imminent, publicly-visible loss-socialization event by racing it with a higher-fee transaction.

### Impact Explanation
This is a "socialize-debt" / "vault share math" issue explicitly within scope. The impact is a transfer of loss away from the front-running depositor onto remaining, honest depositors in the same vault — i.e., theft of value from other users' principal/yield without their consent, and in the worst case (large bad debt relative to pool size) it can push `new-lindex` toward `u0`, causing severe or total impairment of remaining depositors' claims (protocol insolvency for that vault). This falls into the Critical category (protocol insolvency / theft of user funds at rest) or, at minimum, High (temporary/permanent freezing or loss of yield for remaining depositors), matching the categories: "direct theft of user funds ... or protocol insolvency."

### Likelihood Explanation
Likelihood is moderate to high in stress scenarios: bad debt events happen when collateral prices crash and a liquidation cannot fully cover debt (as demonstrated in the repo's own test `ATK-LG-05: Bad debt cannot be artificially created`, which shows the liquidate→socialize-debt path is actively exercised in these scenarios). Any depositor of the affected vault monitoring the mempool for large under-collateralized liquidations (a public, easily detectable event since it requires an oracle price crash) can react by submitting a fee-boosted `redeem()`. No special privileges are required — any principal holding zTokens of the affected vault can execute this.

### Recommendation
- Introduce a withdrawal delay / two-step redeem process (request + claim after a cooldown) similar to the mitigation recommended in the referenced report, so shares cannot be exited within the same block/short window as an in-flight bad-debt liquidation.
- Alternatively, snapshot `total-assets` (and compute `lindex` write-down) at the start of the liquidation-triggering flow (e.g., using a per-block cached total-assets value consistent with the existing per-block index-cache pattern already used for borrow indexes) so that the write-down ratio cannot be affected by redemptions that occur later in the same block.
- Consider applying redemption caps or fees during active bad-debt-socialization events, or defer `redeem()` execution when a `socialize-debt-asset` call is pending in the same block.

### Proof of Concept
1. Vault `V` (e.g. `v0-vault-usdc`) has `total-assets = 1,000,000 USDC`, depositor Alice holds shares worth `100,000 USDC`.
2. Collateral price crashes; a liquidator submits `liquidate()` on `v0-4-market.clar` for a borrower whose debt exceeds their collateral, which will trigger `socialize-debt-asset` with `debt-reduction = 50,000 USDC` [4](#0-3) .
3. Alice observes the pending `liquidate()` transaction in the mempool and submits `redeem()` for her full `100,000 USDC` share with a higher transaction fee [3](#0-2) .
4. Alice's `redeem()` is confirmed first, reducing `total-assets` to `900,000 USDC` before the debt socialization executes.
5. `liquidate()` confirms next; `socialize-debt` computes `new-lindex = lindex * (900,000 - 50,000) / 900,000` instead of `lindex * (1,000,000 - 50,000) / 1,000,000` [5](#0-4) , applying a larger proportional loss to all remaining depositors while Alice exits with her full, unimpaired balance.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L879-903)
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
            (unwrap! (contract-call? .v0-market-vault
                                      debt-remove-scaled
                                      borrower
                                      scaled-debt
                                      asset-id) failed-status)
          acc)
        ))
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

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L795-815)
```text
(define-public (redeem (amount uint) (min-out uint) (recipient principal))
  (let (
    (states (var-get pause-states))
    (u (try! (accrue)))
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
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L942-967)
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
```
