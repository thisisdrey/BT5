## Analysis

The reported issue is a classic "frontrun-the-loss" pattern: a party that can observe an impending devaluation event ahead of time can exit at the pre-loss NAV, while the loss is retroactively socialized onto whoever remains. Zest has a directly analogous mechanism.

**How Zest's vault share price can drop:** each lending vault (`vault-stx.clar`, `vault-ststx.clar`, `vault-usdc.clar`, etc.) tracks value per zToken share via a `lindex` variable. Normally `lindex` only increases (interest accrual). It can be written *down* — the only devaluation path for zToken holders — through `socialize-debt`, which is invoked from `market.clar`'s `liquidate` function when a liquidated position leaves uncovered bad debt: [1](#0-0) 

This is called from the liquidation flow when `no-collateral-left` and there's still unpaid debt: [2](#0-1) 

**Redeem is oblivious to pending socialization:** `redeem` on every vault computes `inkind` from the *current* `lindex` via `convert-to-assets-preview`, with no check for an impending or in-flight bad-debt write-down: [3](#0-2) 

**Price update and liquidation are bundled in one caller-supplied transaction:** `liquidate` accepts `price-feeds` and calls `write-feeds` itself, meaning the price data (e.g., a Pyth price update revealing a large STX/BTC drop that makes a position insolvent) is pushed to the chain in the *same* transaction that performs the liquidation and eventual `socialize-debt` call: [4](#0-3) 

Because that transaction (with its embedded price-feed payload) is visible in the mempool before it lands, any zToken holder in the same vault can observe it, deduce that a bad-debt socialization is imminent, and submit a `redeem` for their own shares with higher priority to be mined first — extracting their exposure at the pre-write-down `lindex`, before the loss is applied. Remaining zToken holders then absorb a disproportionate share of the socialized bad debt.

### Title
Vault depositors can frontrun `socialize-debt` NAV write-downs by redeeming before an in-flight liquidation lands - (File: `mainnet/contracts/vault/v0-vault-stx.clar`, `mainnet/contracts/vault/v0-vault-ststx.clar`, and sibling vaults, together with `mainnet/contracts/market/v0-4-market.clar`)

### Summary
Zest's lending vaults price zTokens via a shared `lindex` that is only ever written down when a liquidation cannot fully cover a borrower's debt (`socialize-debt`). Because the triggering `liquidate` call (including its price-feed update) is a plain, mempool-visible Stacks transaction, and `redeem` performs no check for a pending or same-block socialization, any vault-share holder can watch for the loss-causing liquidation transaction and beat it into the chain with their own `redeem`, exiting at the pre-loss `lindex` and shifting their share of the bad debt onto the remaining depositors.

### Finding Description
`redeem` in each vault (`vault-stx.clar`/`v0-vault-stx.clar`, `vault-ststx.clar`/`v0-vault-ststx.clar`, `vault-usdc.clar`, `vault-usdh.clar`, `vault-sbtc.clar`, `vault-ststxbtc.clar`) computes the underlying amount from the current `lindex` with no awareness of an impending devaluation: [3](#0-2) 

`lindex` is only reduced by `socialize-debt`, which is exclusively invoked from `market.clar`'s `liquidate` when the borrower's remaining collateral cannot cover their debt: [2](#0-1) [1](#0-0) 

`liquidate` itself takes `price-feeds` as an argument and pushes them on-chain via `write-feeds` in the same transaction that performs the liquidation: [5](#0-4) 

Because Stacks transactions sit in a visible mempool before confirmation, any actor watching for a `liquidate` call carrying a price update that will push a large position underwater can determine, ahead of confirmation, that a `socialize-debt` write-down of a specific vault's `lindex` is about to occur. That actor (any existing zToken holder in that vault) can submit a competing `redeem` transaction with higher fee/priority to be included first, cashing out at the un-devalued `lindex` and leaving the remaining zToken holders to absorb the loss — this is structurally identical to the reported LST/LRT redeem-before-devaluation pattern, just replacing "beacon-chain slashing event" with "on-chain liquidation transaction that will trigger `socialize-debt`."

### Impact Explanation
This results in a direct, permanent value transfer: the frontrunning depositor extracts their full share at the pre-loss NAV, and the bad debt that should have been distributed pro-rata across all zToken holders at the time of insolvency is instead concentrated onto whoever remains in the vault, permanently reducing their redeemable balance. This is theft of principal value (funds at rest) from the remaining, honest depositors of that vault.

### Likelihood Explanation
Any sophisticated user (or a bot) simply needs to watch the mempool/network for `liquidate` calls against large, systemically significant positions, or independently track off-chain price feeds (Pyth/DIA) to anticipate when a monitored position will become deeply underwater, then race a `redeem` transaction ahead of the liquidation. No special privileges, DAO compromise, or flashloan primitive is required — only ordinary `redeem`/`liquidate` calls available to any principal.

### Recommendation
Introduce a mechanism that prevents zToken redemptions from bypassing pending or same-block bad-debt socialization — e.g., a withdrawal delay/queue for large redemptions, snapshotting `lindex` at a fixed cadence rather than allowing atomic race conditions, or requiring `redeem` to first check whether any liquidation affecting that vault is pending in the same block and applying socialization before honoring redemptions.

### Proof of Concept
1. Borrower B has a position collateralized in a given vault (e.g., `vault-sbtc`) that becomes severely underwater after a price move.
2. A liquidator (or B's own bot) crafts a `liquidate` call with fresh `price-feeds` proving the underwater state; because bad debt remains after seizing all collateral, this call will invoke `socialize-debt` on the debt vault, marking down that vault's `lindex`. [2](#0-1) 
3. Attacker A, an existing zToken holder of the debt vault, observes this pending `liquidate` transaction in the mempool (or independently observes the underlying price drop before it's pushed on-chain).
4. A submits `redeem` for all of their zTokens with a higher fee to get mined before the `liquidate` transaction. [3](#0-2) 
5. A's `redeem` executes at the old (higher) `lindex`, extracting full value.
6. The subsequent `liquidate` transaction confirms and calls `socialize-debt`, writing `lindex` down for the vault — but now applied over a smaller remaining share supply, so remaining depositors absorb more than their original pro-rata share of the loss. [1](#0-0)

### Citations

**File:** local-testing/contracts/vault/vault-ststx.clar (L801-821)
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

**File:** local-testing/contracts/vault/vault-ststx.clar (L948-970)
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

**File:** local-testing/contracts/market/market.clar (L1411-1432)
```text
                (collateral-receiver (optional principal))
                (price-feeds (optional (list 3 (buff 8192)))))
  (let (
    (feeds-check (try! (write-feeds price-feeds)))
    (liquidator contract-caller)
    (position (try! (get-liquidation-position borrower)))
    (pos-full (try! (get-full-position borrower)))
    (mask (get mask position))
    (group (try! (get-egroup mask)))

    (coll-address (contract-of collateral-ft))
    (debt-address (contract-of debt-ft))
    (coll-asset (try! (get-asset coll-address)))
    (debt-asset (try! (get-asset debt-address)))
    (coll-aid (get id coll-asset))
    (debt-aid (get id debt-asset))

    ;; accrue FIRST - populates cache for zToken price resolution
    (u-debt (accrue-user-debts (get debt pos-full)))
    (u-coll (accrue-user-collateral (get collateral pos-full)))

    ;; NOW safe to resolve prices (cache is populated)
```

**File:** local-testing/contracts/market/market.clar (L1556-1583)
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
