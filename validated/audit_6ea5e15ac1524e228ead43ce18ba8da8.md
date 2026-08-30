### Title
Repay and liquidation both blocked by `debt-remove` / vault `repay` pause flags, preventing unhealthy positions from being unwound - ([File: mainnet/contracts/market/v0-4-market.clar])

### Summary
The `repay()` and `liquidate()` functions in `v0-4-market.clar` do not themselves check a pause flag, but both internally call `debt-remove-scaled` on `v0-market-vault.clar`, which reverts with `ERR-PAUSED` if the `debt-remove` pause state is set. Both functions also call `vault-system-repay`, which forwards into each underlying vault's `system-repay`, which reverts with `ERR-PAUSED` if that vault's `repay` pause flag is set. This reproduces the exact bug class from the external report — a debt-repayment path being gated by a pause flag that a user cannot bypass even when their own position is about to become undercollateralized.

### Finding Description
`repay()` in `v0-4-market.clar` only asserts caller authorization, non-zero amount, and non-zero scaled debt — it has no pause check of its own: [1](#0-0) 

But it unconditionally calls `vault-system-repay` and then `debt-remove-scaled`: [2](#0-1) 

`debt-remove-scaled` in `v0-market-vault.clar` reverts with `ERR-PAUSED` whenever the market-vault's `debt-remove` pause flag is active: [3](#0-2) 

Separately, `vault-system-repay` forwards to each underlying vault's `system-repay`, which independently reverts with `ERR-PAUSED` if that vault's own `repay` pause flag is set: [4](#0-3) 

Critically, `liquidate()` has the *same* dependency chain — it also calls `vault-system-repay` and `debt-remove-scaled` to settle the borrower's debt during liquidation: [5](#0-4) 

So if either (a) a vault's `repay` pause flag, or (b) the market-vault's `debt-remove` pause flag is set, **both** voluntary repayment and third-party liquidation of an unhealthy position become impossible, exactly mirroring the external report's scenario where `lockForAnOrder`'s `whenNotPaused` modifier blocked `increasePosition()`'s ability to let a user pay down debt.

### Impact Explanation
While a pause is active:
- A user with a healthy position that becomes unhealthy due to price movement cannot repay to restore health.
- Liquidators cannot liquidate the position either, since `liquidate()` shares the same paused dependency.
This means bad debt can accumulate unchecked for the duration of the pause with no mechanism to intervene, risking protocol insolvency once the position's collateral value falls below its debt value and remains that way after unpausing (by which time the position may be so far underwater that liquidation cannot make the protocol whole) — this falls under **temporary freezing of funds** for the affected users' assets, and can escalate to **protocol insolvency** if the price continues moving against the position for the pause duration. Confirming whether pausing `debt-remove`/vault `repay` is intended as an emergency admin action (in scope) versus something else could not be fully verified from static code alone, but nothing in the pause-setter functions (`set-pause-states`) indicates DAO-compromise is required — it is a normal operational lever, analogous to the original report's "paused for maintenance/security" scenario.

### Likelihood Explanation
Likelihood is tied entirely to how often/how long the protocol pauses `debt-remove` or a vault's `repay` flag — a legitimate, expected operational action (e.g., during an incident response or maintenance window), not requiring any compromise. Any pause event that coincides with adverse price movement for even one borrower triggers this condition, making it a realistic, medium-likelihood scenario, consistent with the "Medium" severity assigned to the original analog finding.

### Recommendation
Decouple the ability to reduce debt (`repay`, and the debt-settlement leg of `liquidate`) from the pause flags used to halt normal operational flows, or introduce a distinct, more restrictive pause category that never blocks debt-reduction/liquidation paths. At minimum, ensure `liquidate()`'s debt-settlement calls are exempt from `debt-remove`/vault `repay` pause flags so third parties can still liquidate underwater positions and prevent insolvency even while normal user-initiated repay/borrow operations are paused.

### Proof of Concept
1. Alice opens a position with healthy collateralization via `v0-4-market.clar`.
2. DAO (or authorized pauser) calls `set-pause-states` on the relevant vault (e.g., `v0-vault-usdh.clar`) setting `repay: true`, or on `v0-market-vault.clar` setting `debt-remove: true` — both are legitimate operational levers, not requiring compromise. [6](#0-5) 
3. Price of Alice's collateral drops, making her position unhealthy.
4. Alice calls `repay()` on `v0-4-market.clar` to pay down debt — the call reverts with `ERR-PAUSED` inside `debt-remove-scaled` (or inside the vault's `system-repay`). [7](#0-6) 
5. A liquidator attempts to call `liquidate()` on the same position — it also reverts with `ERR-PAUSED` via the same `debt-remove-scaled`/`vault-system-repay` dependency. [8](#0-7) 
6. Alice's bad debt remains open and can worsen for the duration of the pause, with neither self-repayment nor liquidation available to limit protocol exposure.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L1350-1361)
```text
    ;; preconditions
    (asserts! (is-eq contract-caller tx-sender) ERR-AUTHORIZATION)
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)
    (asserts! (> repaid-scaled-debt u0) ERR-INSUFFICIENT-SCALED-DEBT)

    (try! (vault-system-repay asset-id amount-to-repay ft address))
    ;; update
    (try! (contract-call? .v0-market-vault
                            debt-remove-scaled
                            account
                            repaid-scaled-debt
                            asset-id))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1488-1503)
```text
    (asserts! (not (is-liquidation-paused debt-aid)) ERR-LIQUIDATION-PAUSED)
    (asserts! (is-eq contract-caller tx-sender) ERR-AUTHORIZATION)
    (asserts! (> debt-amount u0) ERR-AMOUNT-ZERO)
    (asserts! (> debt-to-repay u0) ERR-ZERO-LIQUIDATION-AMOUNTS)
    (asserts! (> coll-final u0) ERR-ZERO-LIQUIDATION-AMOUNTS)
    (asserts! (>= coll-final min-collateral-expected) ERR-SLIPPAGE)

    ;; execute liquidation
    (try! (vault-system-repay debt-aid debt-to-repay debt-ft debt-address))

    ;; update obligations and socialize bad debt
    (let ((debt-updated (try! (contract-call? .v0-market-vault
                              debt-remove-scaled
                              borrower
                              scaled-to-remove
                              debt-aid)))
```

**File:** mainnet/contracts/market/v0-market-vault.clar (L473-487)
```text
(define-public (debt-remove-scaled (account principal) (scaled-amount uint) (asset-id uint))
  (let ((states (var-get pause-states))
        (entry (resolve account))
        (user-id (get id entry))
        (mask (get mask entry))
        (remaining (try! (remove-user-scaled-debt user-id asset-id scaled-amount)))
        (nmask (if (is-eq remaining u0)
                      (mask-update mask asset-id false false) ;; debt, remove
                      mask))
        (updated-entry (merge entry (refresh nmask))))

    (try! (check-impl-auth))
    (asserts! (not (get debt-remove states)) ERR-PAUSED)
    (asserts! (> scaled-amount u0) ERR-AMOUNT-ZERO)

```

**File:** mainnet/contracts/vault/v0-vault-usdh.clar (L900-921)
```text
(define-public (system-repay (amount uint))
  (let (
        (states (var-get pause-states))
        (u (try! (accrue)))
        (scaled-principal (var-get principal-scaled))
        (idx (var-get index))
        (debt (total-debt))
        (total-borrowed-amount (var-get total-borrowed))
        (capped-amount (if (> amount debt) debt amount))
        (principal-reduction (calc-principal-ratio-reduction capped-amount scaled-principal debt))
        (capped-reduction (if (> principal-reduction scaled-principal) scaled-principal principal-reduction))
        (updated-scaled-principal (- scaled-principal capped-reduction))
        (principal-repaid (mul-div-down capped-amount total-borrowed-amount debt))
        (interest-paid (- capped-amount principal-repaid))
        (total-borrowed-new (if (> total-borrowed-amount principal-repaid) (- total-borrowed-amount principal-repaid) u0)))

    (try! (check-caller-auth))
    (asserts! (not (get repay states)) ERR-PAUSED)
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)

    (try! (receive-underlying capped-amount tx-sender))
    (var-set principal-scaled updated-scaled-principal)
```
