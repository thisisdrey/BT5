### Title
LP depositors can front-run liquidation-triggered bad-debt socialization to avoid vault share-price losses - (File: `mainnet/contracts/market/v0-4-market.clar`, `mainnet/contracts/vault/v0-vault-usdc.clar` and sibling vaults)

### Summary
Zest's vaults share the same loss-socialization pattern as InfiniFi's `StakedToken.accrue`: when a liquidation leaves a borrower with no collateral, `liquidate()` calls `socialize-debt-asset` for each remaining debt asset, which invokes the debt vault's `socialize-debt` function and instantly writes down `lindex` (the liquidity/loss index) proportionally to the bad debt [1](#0-0) . Because the vault's share price (`convert-to-assets-preview`) is a pure function of `total-assets`/`total-supply` with no delay or lock-up on `redeem()` [2](#0-1) , an LP who observes an under-collateralized position becoming liquidatable (all state is public: prices, LTV, position data) can submit a `redeem()` transaction with a higher fee to be mined before the pending `liquidate()` transaction that performs `socialize-debt`, thereby exiting at the pre-loss share price and avoiding their pro-rata share of the bad debt.

### Finding Description
`socialize-debt` in each vault (`v0-vault-usdc.clar`, `v0-vault-sbtc.clar`, etc.) reduces `lindex` proportionally to the loss in `total-assets`, which lowers the effective value backing every outstanding vault share for LPs still holding shares at the time it executes [3](#0-2) . This write-down is triggered non-atomically from the LP's perspective — it is invoked from the `liquidate()` flow in the market contract only when `no-collateral-left` is true, at line 1534-1560, and any LP can watch prices/positions and predict when this branch will fire for a bad-debt position [1](#0-0) . Meanwhile, `redeem()` computes the payout using the current (pre-loss) `convert-to-assets-preview`, which only reflects `total-assets`/`total-supply` at the moment it executes, with no minimum holding period, no queued withdrawal delay, and no penalty for redeeming right before a loss event [4](#0-3) . This is functionally identical to the reported StakedToken issue: `accrue`/`applyLosses` write down value for holders, but a holder can front-run and redeem beforehand to dodge the loss.

### Impact Explanation
This lets sophisticated/first-mover LPs consistently avoid the socialized-loss portion of bad debt that the protocol is designed to distribute across all LPs of a given asset vault, shifting a larger share of the loss onto slower or passive LPs. This is a permanent loss/misallocation of funds among LP depositors — it does not create new funds, but it results in unclaimed-loss-avoidance for the front-runner at the direct expense of remaining depositors, matching the "temporary/permanent freezing/misallocation of funds" and "theft" categories relevant to unprivileged principals interacting with vault share math and socialize-debt accounting (in-scope categories: vault share math and socialize-debt).

### Likelihood Explanation
Likelihood is high in adversarial/competitive environments: liquidatable positions and their impending bad debt are fully visible on-chain (price oracle updates, position LTV, collateral remaining), and Stacks transactions are ordered by fee within a block, enabling straightforward priority-fee front-running of the `liquidate()` call that triggers `socialize-debt`. No special privilege or flashloan is required — any LP with vault shares can execute `redeem()` themselves.

### Recommendation
Add a mechanism analogous to InfiniFi's fix: enforce a minimum holding/cooldown period for vault shares before `redeem()`/`withdraw()` is permitted, or revert `redeem()` when there are pending unaccrued losses/bad debt to be socialized for the underlying asset (e.g., check that no bad-debt-socialization is queued/pending against the vault before allowing withdrawal), removing the ability to exit at a stale share price immediately ahead of a loss-realizing liquidation.

### Proof of Concept
1. Attacker (an ordinary LP) deposits into `vault-usdc` and holds `zUSDC` shares, or acquires shares on secondary market.
2. Attacker monitors a borrower position becoming eligible for full liquidation with no collateral left (LTV, price feed data are public via `get-egroup`/`get-liquidation-position`).
3. As soon as a `liquidate()` transaction that will trigger `no-collateral-left` and hence `socialize-debt-asset` (mainnet/contracts/market/v0-4-market.clar lines 1534-1560) is broadcast/predictable (e.g., anyone can call `liquidate()` on the visible unhealthy position), the attacker submits a `redeem()` call to `vault-usdc` with a higher transaction fee.
4. The attacker's `redeem()` is mined first, computing payout via `convert-to-assets-preview` at the pre-loss `total-assets`/`lindex` [4](#0-3) .
5. The `liquidate()` transaction is then mined, calling `socialize-debt`, which writes down `lindex` for all remaining shareholders [3](#0-2) .
6. The attacker has fully avoided their share of the bad-debt loss, which is now absorbed entirely by the LPs who did not redeem in time.

### Citations

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

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L319-326)
```text
        u0
        (if (is-eq ts u0)
            u0
            (mul-div-down amount ta ts)))))

;; -- Debt helpers -----------------------------------------------------------

(define-private (total-debt)
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L799-833)
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
    action: "redeem",
    caller: contract-caller,
    data: {
      redeemer: account,
      recipient: recipient,
      shares-burned: amount,
      amount-received: inkind,
      assets: (- current-assets inkind)
    }
  })

  (ok inkind)))

;; -- Lending operations -----------------------------------------------------

(define-public (accrue)
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L946-970)
```text
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
      data: {
        scaled-amount: scaled-amount,
```
