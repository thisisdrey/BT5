### Title
Borrowers can self-liquidate their own position to extract the liquidation bonus and bypass the borrow-health withdrawal limit, draining collateral and pushing remaining debt into bad-debt socialization - (File: `mainnet/contracts/market/v0-4-market.clar`)

### Summary
`liquidate()` in `v0-4-market.clar` never checks that the caller (`liquidator = contract-caller`) is different from the `borrower` being liquidated. This lets any borrower call `liquidate` against their own position once it crosses `LTV-LIQ-PARTIAL`, repay their own debt, and receive back their own collateral inflated by the `liq-penalty` bonus - extracting value that a normal `repay`/`collateral-remove` flow would never allow, and that is only supposed to compensate a risk-bearing third-party liquidator.

### Finding Description
In `liquidate`, the liquidator identity is simply `contract-caller` with no restriction against it being equal to `borrower`: [1](#0-0) 

The only preconditions are that the position is unhealthy (`current-ltv >= ltv-liq-partial`) and not in the same block as the last borrow: [2](#0-1) 

The collateral seized is deliberately inflated by `liq-penalty` relative to the debt repaid (`calc-liq-collateral-repay`): [3](#0-2) 

And the receiver of that inflated collateral defaults to the liquidator (`contract-caller`) when no explicit receiver is passed: [4](#0-3) 

By contrast, the normal debt/collateral-management path enforces a strictly tighter and premium-free rule: `repay` reduces debt 1:1 with no health check at all, and `collateral-add`/`collateral-remove`/`borrow` only allow withdrawing/borrowing while respecting the conservative `LTV-BORROW` threshold (via `is-healthy-with-mask`) with **no bonus**: [5](#0-4) 

Because `liquidate` requires only crossing `LTV-LIQ-PARTIAL` (a much looser bound than `LTV-BORROW`) and grants a `liq-penalty` bonus on the collateral withdrawn, a borrower who lets (or engineers, e.g. via a price move or simply borrowing up to the partial-liquidation edge and waiting for interest accrual) their own position cross `LTV-LIQ-PARTIAL` can call `liquidate` on themselves repeatedly: each call repays some of their own debt but withdraws collateral valued at `debt_repaid_usd * (1 + liq_penalty)`, i.e. strictly more collateral value than the debt value cleared. This is functionally identical to the StakeWise analog where `_redeemOsToken`'s liquidation-bonus path (`VaultOsToken.sol`) let a staker recover more ETH per osETH share burned than a normal redemption, purely because the liquidation code path rewards the caller with a bonus not present in ordinary debt-reduction paths, and nothing enforces that the liquidator is a distinct, risk-bearing third party.

Because each self-liquidation call removes collateral disproportionately faster than debt, the position's LTV does not improve as intended and can be pushed to have zero collateral remaining while debt is still outstanding, at which point the contract's own bad-debt socialization logic kicks in and spreads the unrepaid debt across the vault (i.e., onto other lenders/depositors): [6](#0-5) 

### Impact Explanation
This is theft of protocol/depositor funds and can cause protocol insolvency: the self-liquidating borrower extracts collateral value above what is due for the debt they clear (the `liq-penalty` bonus, up to `LIQ-PENALTY-MAX`), and can iterate this until their collateral is exhausted while remaining debt is written off via `socialize-debt-asset`, transferring the loss to other depositors in the affected vault(s). This satisfies the in-scope "Critical - direct theft of user funds at rest ... or protocol insolvency" impact class, since it is lender/depositor capital that is ultimately socialized to cover the artificially-inflated withdrawal the self-liquidator took.

### Likelihood Explanation
Any ordinary borrower can trigger this without special privileges or DAO involvement: they simply need their own position's LTV to reach `LTV-LIQ-PARTIAL` (achievable by borrowing near the limit and letting interest accrue, or via ordinary price movement) and then call `liquidate(borrower=self, ...)` from their own EOA or a deployed contract. No flashloan, oracle manipulation, or leaked key is required, making this directly reachable and repeatable (bounded only by `same-block` protection and needing the position to remain above `LTV-LIQ-PARTIAL` between calls, which is inherently satisfied because each self-liquidation call worsens rather than improves the true collateral/debt ratio backing the debt).

### Recommendation
Add an explicit check in `liquidate` (and `liquidate-multi`/`liquidate-redeem`) that `contract-caller` (the liquidator) is not equal to `borrower`, e.g. `(asserts! (not (is-eq contract-caller borrower)) ERR-SELF-LIQUIDATION)`, mirroring how StakeWise-class protocols must restrict liquidation-bonus paths to genuine third parties.

### Proof of Concept
1. Alice deposits collateral and borrows against it up to just under `LTV-BORROW`.
2. Alice waits for interest accrual (or a modest price move) to push her `current-ltv` at or above `LTV-LIQ-PARTIAL` (per `docs/market.md` example, `LTV-LIQ-PARTIAL` ≈ 85% while `LTV-BORROW` ≈ 75%, so this is a much easier bar than becoming healthy again for a normal withdrawal). [7](#0-6) 
3. Alice (as `tx-sender`/`contract-caller`) calls `liquidate(borrower: Alice, collateral-ft, debt-ft, debt-amount, min-collateral-expected, collateral-receiver: none, price-feeds: none)`.
4. `liquidator` resolves to Alice's own principal; the health check only requires `current-ltv >= ltv-liq-partial` and does not reject `borrower == liquidator`. [8](#0-7) 
5. `process-collateral-asset`/`calc-liq-collateral-repay` compute collateral to seize as `debt_repaid_usd * (1 + liq_penalty) / price`, so Alice receives back collateral worth more than the debt she just repaid, directly to herself (`actual-receiver` defaults to `liquidator` = Alice). [3](#0-2) 
6. Alice repeats step 3 across multiple transactions (blocked only from doing it in the same block as her last borrow) until her collateral is fully withdrawn while debt remains, at which point `no-collateral-left` triggers `socialize-debt-asset`, socializing her unpaid debt onto the vault's other depositors. [6](#0-5)

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L663-666)
```text
(define-private (is-healthy-with-mask (collateral-usd uint) (debt-usd uint) (mask uint))
  (let ((group (try! (get-egroup mask)))
        (ltvb (buff-to-uint-be (get LTV-BORROW group))))
    (ok (is-healthy collateral-usd debt-usd ltvb))))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L726-734)
```text
;; Calculate collateral to seize (includes liquidator bonus)
;; collateral-repay = debt-repay * (BPS + liq-penalty) / BPS
(define-private (calc-liq-collateral-repay (debt-repay uint) (liq-penalty uint)) 
  (mul-bps-down debt-repay (+ BPS liq-penalty)))

;; Calculate actual debt repayment when collateral is capped
;; debt-repay-real = (collateral-amount-usd * BPS) / (BPS + liq-penalty)
(define-private (calc-liq-debt-repay-real (collateral-amount-usd uint) (liq-penalty uint)) 
  (div-bps-down collateral-amount-usd (+ BPS liq-penalty)))
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

**File:** mainnet/contracts/market/v0-4-market.clar (L1428-1435)
```text
    ;; Oracle frontrunning protection: prevent same-block liquidation
    ;; This blocks flash-loan based attacks where user borrows + gets liquidated in same block
    (last-borrow-block (get last-borrow-block position))
    (same-block-check (asserts! (not (is-eq last-borrow-block stacks-block-height)) ERR-LIQUIDATION-BORROW-SAME-BLOCK))

    ;; health check (FAIL-FAST) 
    ;; Check position is liquidatable BEFORE calling calc-liq-factor
    (health-check  (asserts! (>= current-ltv ltv-liq-partial) ERR-HEALTHY))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1524-1531)
```text
                (mul-div-up other-scaled other-borrow-idx INDEX-PRECISION))
              u0))
          (no-collateral-left (and
                                (is-eq coll-removed u0)
                                (or
                                  (is-eq (len (get collateral pos-full)) u1)
                                  (and
                                    (is-eq (len (get collateral pos-full)) (len (get collateral position)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1549-1583)
```text
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
        
        ;; emit main liquidate event
        (print {
          action: "liquidate",
          caller: contract-caller,
          data: {
            liquidator: liquidator,
            borrower: borrower,
            collateral-asset-id: coll-aid,
            collateral-asset-addr: coll-address,
            debt-asset-id: debt-aid,
            debt-asset-addr: debt-address,
            debt-repaid: debt-to-repay,
            debt-repaid-usd: debt-final-usd,
            collateral-seized: coll-final,
            collateral-price: coll-price,
            collateral-decimals: coll-decimals,
            liq-penalty-bps: liq-penalty,
            position-collateral-usd-before: total-collateral-usd,
            position-debt-usd-before: total-debt-usd,
            bad-debt-socialized: bad-debt-socialized
          }
        })
```

**File:** docs/market.md (L216-234)
```markdown
### LTV Thresholds

Egroups define three LTV levels:

1. **LTV-BORROW (e.g., 75%)**
   - Used for new borrows and withdrawals
   - Most conservative threshold
   - Ensures buffer before liquidation

2. **LTV-LIQ-PARTIAL (e.g., 85%)**
   - Triggers partial liquidation
   - Allows targeted debt reduction
   - Minimizes user losses

3. **LTV-LIQ-FULL (e.g., 95%)**
   - Triggers full liquidation
   - Position is severely undercollateralized
   - Liquidator can clear entire position

```
