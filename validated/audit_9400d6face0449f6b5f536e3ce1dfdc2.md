### Title
Front-running `repay` with a `borrow` forces a payer to pay more than intended - ([File: mainnet/contracts/market/v0-4-market.clar])

### Summary
The `repay` function in `v0-4-market.clar` computes the amount actually pulled from the payer based on the *current, on-chain* debt balance of the target account at the time the transaction is mined, not the balance the payer observed when they signed the transaction. Because `repay` lets a caller repay on behalf of any account (`on-behalf-of`) and clamps the requested `amount` up to whatever the account's live debt turns out to be, a borrower can watch an incoming repay in the mempool and front-run it with an additional `borrow`, inflating their own debt so that the payer's transaction is forced to transfer more tokens than they intended.

### Finding Description
`repay` resolves the amount to actually collect as: [1](#0-0) 

```
(account-scaled-debt (get-account-scaled-debt account asset-id))
(max-repay-tokens (mul-div-up account-scaled-debt borrow-index INDEX-PRECISION))
(safe-amount (min amount max-repay-tokens))
(scaled-debt-repayment (mul-div-down safe-amount INDEX-PRECISION borrow-index))
(repaid-scaled-debt (min account-scaled-debt scaled-debt-repayment))
(amount-to-repay (mul-div-up repaid-scaled-debt borrow-index INDEX-PRECISION))
```

`max-repay-tokens` is derived from `account-scaled-debt`, which is read live from storage at execution time [2](#0-1) . There is no upper-bound check comparing `amount-to-repay` against a value the payer explicitly approved beyond the caller-supplied `amount` parameter itself; if the payer supplies a very large `amount` (the idiomatic "repay everything" pattern, analogous to `uint(-1)` in the Solidity report), `safe-amount` collapses to `max-repay-tokens`, i.e., whatever the account's debt is *at mining time*.

Critically, `repay` allows the caller to specify an arbitrary `on-behalf-of` account: [3](#0-2) 

```
(define-public (repay (ft <ft-trait>) (amount uint) (on-behalf-of (optional principal)))
  (let ((address (contract-of ft))
        (asset (try! (get-asset address)))
        (asset-id (get id asset))
        ;; defaults to payer (contract-caller) if not specified
        (account (match on-behalf-of behalf behalf contract-caller))
```

while the tokens are always pulled from `contract-caller`/`tx-sender` via `vault-system-repay` (which invokes the `ft` transfer from the payer) — the same authorization check appears elsewhere in the contract, e.g. `(asserts! (is-eq contract-caller tx-sender) ERR-AUTHORIZATION)` in the neighboring `supply-collateral-add`. This means Alice (the payer) can call `repay(ft, MAX, (some bob))` intending to clear Bob's currently-known debt. Bob observes this pending transaction and front-runs it with `borrow`, increasing `account-scaled-debt` for himself before Alice's `repay` is mined. Since `borrow` itself has no dependency on a stale repay computation and simply increases `get-account-scaled-debt` [4](#0-3) , by the time Alice's `repay` executes, `max-repay-tokens` reflects Bob's *inflated* debt, and Alice's tokens are used to repay the larger amount — exactly the class of exploit in the report (front-running `repay` with `borrow`).

### Impact Explanation
This is a direct theft-of-funds vector: an unprivileged principal (the borrower) can, without any protocol misconfiguration or privileged access, force another unprivileged principal (a third-party payer using the on-behalf-of repay feature, or a payer submitting a "repay all" transaction while unaware of intervening state changes) to transfer more of their own tokens into the protocol than intended, with the excess directly benefiting the borrower's position (their inflated debt gets extinguished by someone else's funds). This falls under "direct theft of user funds at rest or in motion."

### Likelihood Explanation
Exploitation requires the attacker to observe a pending `repay` call (mempool visibility) and successfully front-run it with a `borrow` call before the `repay` is mined — a purely mechanical, unprivileged, deterministic front-running operation with no oracle, DAO, or privileged dependency. It is most reliably triggered when a third party repays "the entire loan" on someone else's behalf using a large `amount`, which is the documented/expected usage pattern for full repayment given the `min` clamping logic.

### Recommendation
Require the payer to specify (and have the contract enforce) an upper bound on `amount-to-repay` that reverts if the live debt is larger than expected, rather than silently clamping to whatever debt currently exists on-chain. Alternatively, disallow use of very large sentinel `amount` values as an implicit "repay entire debt," and instead require repay amount to be pre-computed and strictly validated against a maximum acceptable value supplied by the caller.

### Proof of Concept
1. Bob has a borrow position.
2. Alice submits `repay(usdcFt, MAX_UINT, (some bob))` intending to clear Bob's currently known 1,000 USDC debt.
3. Bob observes Alice's pending transaction and submits `borrow(usdcFt, 1000, ...)` with higher priority fee, increasing his own `account-scaled-debt` before Alice's transaction is mined [5](#0-4) .
4. Alice's `repay` is mined afterward; `account-scaled-debt` for Bob now reflects 2,000 USDC of debt, so `max-repay-tokens` and consequently `amount-to-repay` reflect 2,000 USDC [6](#0-5) .
5. `vault-system-repay` pulls 2,000 USDC from Alice instead of the 1,000 USDC she intended, and Bob's inflated debt is fully cleared at Alice's expense.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L1238-1296)
```text
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
        (assets (get-assets mask))

        ;; Calculate current health with current mask
        (current-group (try! (get-egroup mask)))
        (current-ltvb (buff-to-uint-be (get LTV-BORROW current-group)))

        ;; LTV
        (notional-valued-assets (get-notional-evaluation { position: position, assets: assets }))
        (collateral-value (get collateral notional-valued-assets))
        (debt-value (get debt notional-valued-assets)))

    ;; preconditions
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)
    (asserts! (get debt asset) ERR-BORROW-DISABLED)
    (asserts! (is-healthy collateral-value debt-value current-ltvb) ERR-UNHEALTHY)

    ;; Calculate FUTURE debt (after adding this debt)
    ;; For debt: bit position = asset-id + 64 (DEBT-OFFSET)
    (let ((future-mask (bit-or mask (pow u2 (+ asset-id DEBT-OFFSET))))
          (future-group (try! (get-egroup future-mask)))
          ;; Per-egroup borrow disable check (uses FUTURE egroup, not current)
          ;; Each bit in BORROW-DISABLED-MASK corresponds to a debt asset ID (NOT offset by 64)
          (disabled-borrow-mask (get BORROW-DISABLED-MASK future-group))
          (debt-increase (try! (get-asset-value asset amount true)))
          (debt-post-increased (+ debt-value debt-increase)))

    ;; Check if this specific asset is disabled for borrowing in the FUTURE egroup
    (asserts! (is-eq (bit-and disabled-borrow-mask (pow u2 asset-id)) u0) ERR-EGROUP-ASSET-BORROW-DISABLED)
    ;; postconditions
    (asserts! (try! (is-healthy-with-mask collateral-value debt-post-increased future-mask)) ERR-UNHEALTHY)

    (try! (vault-system-borrow asset-id amount funds-receiver))
    (let ((scaled-debt-added (convert-to-scaled-debt asset-id amount true))
          (borrow-index (get index (unwrap-panic (get-cached-indexes asset-id)))))
      (try! (contract-call? .v0-market-vault
                            debt-add-scaled
                            account
                            scaled-debt-added
                            asset-id))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1316-1321)
```text
(define-public (repay (ft <ft-trait>) (amount uint) (on-behalf-of (optional principal)))
  (let ((address (contract-of ft))
        (asset (try! (get-asset address)))
        (asset-id (get id asset))
        ;; defaults to payer (contract-caller) if not specified
        (account (match on-behalf-of behalf behalf contract-caller))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1332-1346)
```text
        ;; Step 3: Get account debt FIRST to enable safe amount capping
        (account-scaled-debt (get-account-scaled-debt account asset-id))
        
        ;; Step 4: Calculate max repayable amount (actual debt in token), mul-div-up for safe upper bound
        (max-repay-tokens (mul-div-up account-scaled-debt borrow-index INDEX-PRECISION))
        
        ;; Step 5: Cap input amount at actual debt - prevents overflow in scaled calculation
        (safe-amount (min amount max-repay-tokens))
        
        ;; Step 6: Convert to scaled debt (amount is bounded)
        (scaled-debt-repayment (mul-div-down safe-amount INDEX-PRECISION borrow-index))

        (repaid-scaled-debt (min account-scaled-debt scaled-debt-repayment))
        (amount-to-repay (mul-div-up repaid-scaled-debt borrow-index INDEX-PRECISION))
        
```
