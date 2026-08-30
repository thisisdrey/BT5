### Title
Same-block borrow-then-repay allows a malicious actor to zero-cost sandwich pool liquidity and DoS all legitimate borrows - (File: `mainnet/contracts/vault/v0-vault-usdc.clar`, `mainnet/contracts/market/v0-4-market.clar`)

### Summary
Zest's per-asset vaults gate `system-borrow` with a hard liquidity check (`<= amount available-assets`), and interest accrual is strictly time-based (driven by elapsed time since `last-update`). Because interest for a loan opened and closed within the same accrual window is ~0, an attacker can borrow the entire available pool liquidity and repay it immediately at negligible cost (gas + normal collateralization only), repeatedly denying legitimate borrowers access to the pool — the same root-cause pattern as the referenced Beedle report (zero-fee borrow/repay cycle abused to DoS the pool).

### Finding Description
`market.clar`'s `borrow` entrypoint calls `vault-system-borrow`, which is enforced in each vault by: [1](#0-0) 
```
(define-public (system-borrow (amount uint) (receiver principal))
  ...
    (asserts! (<= amount available-assets) ERR-INSUFFICIENT-VAULT-LIQUIDITY)
    (asserts! (<= (+ debt amount) CAP-DEBT) ERR-DEBT-CAP-EXCEEDED)
    (var-set principal-scaled updated-scaled-principal)
    (var-set total-borrowed (+ (var-get total-borrowed) amount))
    (try! (send-underlying amount receiver))
```
A malicious actor can front-run a legitimate borrower by calling `borrow` for the full `available-assets` of a vault, which succeeds because there is no minimum-hold-time or per-block borrow limit. The legitimate borrower's transaction, executed after, hits `ERR-INSUFFICIENT-VAULT-LIQUIDITY` and reverts.

Interest is only accrued as a function of elapsed time since `last-update`, computed in `accrue`: [2](#0-1) 
```
(define-public (accrue)
  ...
      (let ((next (next-index))
            (nliq (next-liquidity-index))
            ...
            (debt-delta (if (> new-debt old-debt) (- new-debt old-debt) u0))
            (reserve-inc (mul-div-down debt-delta (var-get fee-reserve) BPS))
            ...
```
If the attacker repays the borrowed amount (via `market`'s `repay`, which calls `system-repay`) within the same block/accrual window as the borrow, `debt-delta` (and therefore `reserve-inc`/protocol fee accrual and lender interest) is effectively zero, mirroring the Beedle report's `timeElapsed == 0` scenario. This makes the full borrow→repay round trip essentially free apart from gas and the collateral the attacker must post to satisfy health checks in `borrow`: [3](#0-2) 
```
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)
    (asserts! (get debt asset) ERR-BORROW-DISABLED)
    (asserts! (is-healthy collateral-value debt-value current-ltvb) ERR-UNHEALTHY)
    ...
    (try! (vault-system-borrow asset-id amount funds-receiver))
```
Since collateral posted is returned in full immediately upon repayment (no time-based cost was incurred), an attacker who has (or flash-borrows) sufficient collateral of an accepted egroup asset can repeatedly execute this borrow/repay cycle every block, permanently starving the vault's `available-assets` for other borrowers whenever their transaction lands after the attacker's `borrow` and before the attacker's `repay`.

### Impact Explanation
This is a temporary freezing-of-funds vector: ordinary users are unable to draw liquidity from an affected vault while the attacker cycles the pool empty and refills it, block after block. This matches the in-scope "temporary freezing of funds" High-impact category, since borrow — one of the protocol's core money-market functions — can be persistently denied for any/all supported assets at low, repeatable cost to the attacker.

### Likelihood Explanation
Likelihood is moderate: the attack requires the attacker to have (or acquire, e.g. via a flashloan used purely as capital) sufficient collateral to satisfy the egroup LTV for the asset being griefed, and to be able to sequence borrow then repay within the same accrual window (single block or narrow time window) so that no meaningful interest/fee cost accrues. This is easier for shallow or newly-launched vaults with large single blocks of unused liquidity, and the attacker only pays gas plus (if borrowing collateral) flashloan fees, since the underlying collateral capital is never actually at risk beyond the round trip.

### Recommendation
Introduce a minimum holding period or a per-block/per-principal borrow-then-repay cooldown so that a full loan lifecycle cannot complete cost-free within the same accrual window; alternatively, apply a minimum floor to accrued interest/fees on `system-borrow`/`system-repay` (e.g., charge at least one unit of the minimum accrual period) independent of `timeElapsed`, so that same-block round trips are not economically free, consistent with the mitigation the original report recommends (attribute a default/minimum fee so the DoS is not cost-free).

### Proof of Concept
1. Attacker deposits collateral (owned or flash-borrowed) sufficient to satisfy `is-healthy` for `future-mask` in `market.clar`'s `borrow` at `mainnet/contracts/market/v0-4-market.clar:1238-1296`.
2. Attacker calls `borrow` for `amount == available-assets` of the target vault (e.g. `v0-vault-usdc`), which passes the `<= amount available-assets` check at `mainnet/contracts/vault/v0-vault-usdc.clar:882`, draining the pool.
3. A legitimate user's `borrow` transaction, ordered after step 2 within the same block, fails with `ERR-INSUFFICIENT-VAULT-LIQUIDITY`.
4. In the same block/session, attacker calls `repay`/`system-repay` to return the borrowed amount; because `accrue`'s `debt-delta` at `mainnet/contracts/vault/v0-vault-usdc.clar:846` is ~0 (no elapsed time), the round trip costs the attacker only gas (plus flashloan fee if collateral was flash-borrowed).
5. Attacker repeats steps 2–4 every block, indefinitely denying borrow access to legitimate users of that vault.

### Citations

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L833-861)
```text
(define-public (accrue)
  (let ((states (var-get pause-states))
        (idx (var-get index))
        (lidx (var-get lindex)))
      (if (get accrue states)
          ;; PAUSED: Pass-through without reverting
          (ok { index: idx, lindex: lidx })
          ;; NOT PAUSED: Normal accrual logic
          (let ((next (next-index))
                (nliq (next-liquidity-index))
                (scaled-principal (var-get principal-scaled))
                (old-debt (mul-div-down scaled-principal idx INDEX-PRECISION))
                (new-debt (mul-div-down scaled-principal next INDEX-PRECISION))
                (debt-delta (if (> new-debt old-debt) (- new-debt old-debt) u0))
                (reserve-inc (mul-div-down debt-delta (var-get fee-reserve) BPS))
                (treasury-lp (if (> reserve-inc u0) (mul-div-down reserve-inc (total-supply) (- (total-assets-preview) reserve-inc)) u0)))
            (if (not (is-eq idx next))
                (var-set index next)
                false)
            (if (not (is-eq lidx nliq))
                (var-set lindex nliq)
                false)
            (if (> treasury-lp u0)
                (try! (ft-mint? zft treasury-lp .dao-treasury))
                false)
            (if (or (not (is-eq idx next)) (not (is-eq lidx nliq)))
                (var-set last-update stacks-block-time)
                false)
            (ok { index: next, lindex: nliq })))))
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L863-883)
```text
(define-public (system-borrow (amount uint) (receiver principal))
  (let (
      (states (var-get pause-states))
      (u (try! (accrue)))
      (CAP-DEBT (var-get cap-debt))
      (available-assets (get-available-assets))
      (scaled-principal (var-get principal-scaled))
      (idx (var-get index))
      (debt (total-debt))
      (scaled-amount (mul-div-up amount INDEX-PRECISION idx))
      (updated-scaled-principal (+ scaled-principal scaled-amount)))

    (try! (check-caller-auth))
    (asserts! (not (get borrow states)) ERR-PAUSED)
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)
    (asserts! (<= amount available-assets) ERR-INSUFFICIENT-VAULT-LIQUIDITY)
    (asserts! (<= (+ debt amount) CAP-DEBT) ERR-DEBT-CAP-EXCEEDED)

    (var-set principal-scaled updated-scaled-principal)
    (var-set total-borrowed (+ (var-get total-borrowed) amount))
    (try! (send-underlying amount receiver))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1269-1296)
```text
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
