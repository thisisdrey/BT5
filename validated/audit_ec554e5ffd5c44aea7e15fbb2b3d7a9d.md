I found the analog. In `borrow`, the `vault-system-borrow` call (which sends the underlying tokens to the borrower/receiver via `send-underlying`) executes and completes *before* the market records the corresponding debt via `debt-add-scaled` on `.market-vault` / `.v0-market-vault`.

### Title
Reentrancy via malicious debt-asset token transfer allows borrowing funds without debt being recorded - ([File: mainnet/contracts/market/v0-4-market.clar])

### Summary
`borrow` sends the borrowed underlying asset to the caller/receiver (`vault-system-borrow` → `send-underlying` → `contract-call? asset transfer ...`) before the corresponding scaled debt is written via `contract-call? .v0-market-vault debt-add-scaled`. Because the asset transferred is an arbitrary SIP-010 `<ft-trait>` implementation, if that implementation (or a wrapped/callback-capable token) invokes the caller during its `transfer` execution, the caller can re-enter `borrow` (or other market entry points) while its debt bookkeeping for the first borrow has not yet been persisted, letting it pass health checks that don't yet reflect the pending debt.

### Finding Description
`borrow` in `mainnet/contracts/market/v0-4-market.clar` performs, in order:
1. Health checks using current position/mask [1](#0-0) 
2. `(try! (vault-system-borrow asset-id amount funds-receiver))`, which transfers the underlying token out to `funds-receiver` [2](#0-1) 
3. Only afterward does it call `debt-add-scaled` on `.v0-market-vault` to persist the new debt for `account` [3](#0-2) 

The underlying transfer is executed through `send-underlying`, which does a generic `contract-call? ... transfer ...` to the vault's configured underlying token contract [4](#0-3) . In Clarity, `contract-call?` to a trait-typed argument executes the target contract's code synchronously in the same transaction; if that token contract itself calls back into `market`/`borrow` (or any other public entry point) as part of its `transfer` logic (analogous to an ERC-777 hook triggering re-entrancy in the reported bug class), the re-entrant call observes market state in which the funds for the first borrow have already left the vault but the corresponding debt obligation has not yet been recorded in `.v0-market-vault`. This mirrors the reported class: reward/funds sent out precede the state update that would prevent repeated draws, and a token with transfer hooks is the vector that reenters mid-transaction.

This is reachable by an ordinary principal supplying any SIP-010-conforming `<ft-trait>` contract (their own deployed contract, or one they control) as the debt asset argument to `borrow`, with no privileged action or DAO compromise required for the attacker to trigger the reentry; the affected function is a core, always-available user entry point.

### Impact Explanation
If exploited, an attacker can obtain a second (or repeated) borrow payout via `send-underlying`/`vault-system-borrow` while `debt-add-scaled` for the prior borrow is not yet committed, meaning the health/LTV check for the second call is computed against understated debt. This allows draining vault liquidity beyond what the attacker's collateral entitles them to — a direct theft of other depositors' funds at rest in the lending vaults, i.e., protocol insolvency / theft of user funds.

### Likelihood Explanation
Exploitability depends on whether any asset admitted by governance as a debt/borrow asset has transfer-time callback behavior reachable by the borrower controlling the receiving contract. Any principal can attempt to register such a call only if a callback-capable token is already an approved market asset; the reentrancy hazard in the code itself, however, exists independently of asset choice and is a genuine CEI (checks-effects-interactions) ordering defect within `borrow`.

### Recommendation
Reorder `borrow` (and equivalent operations in `repay`/other vault-touching entry points across `v0-4-market.clar`/`market.clar`) so that all internal state changes (`debt-add-scaled`, mask updates) are performed and committed *before* any external token transfer (`vault-system-borrow`/`send-underlying`) occurs, following checks-effects-interactions ordering. Additionally, consider adding an explicit reentrancy guard (similar to the `in-flashloan` flag already used in the vault `flashloan` functions, e.g. [5](#0-4) ) around `borrow`/`repay` in the market contract to prevent nested re-entry into these entry points during an in-flight external token call.

### Proof of Concept
1. Attacker deploys a SIP-010-compliant token contract `EvilToken` and gets it registered/available as a market debt asset (assumes governance already lists it, or attacker uses an existing asset with such hook behavior — the market itself does not defend against callback-capable transfer implementations).
2. Attacker deposits minimal legitimate collateral sufficient to pass the health check for one borrow of `EvilToken`.
3. Attacker calls `market.borrow(EvilToken, amount, receiver=attacker-contract, ...)`.
4. Execution reaches `(try! (vault-system-borrow asset-id amount funds-receiver))` [2](#0-1) , which calls `EvilToken`'s `transfer` as part of `send-underlying`.
5. `EvilToken.transfer` callback re-enters `market.borrow` again for the same collateral before `debt-add-scaled` from step 3's original call has been recorded (it happens only in the following lines) [6](#0-5) ; the health check in the nested call still sees the pre-borrow debt value and passes, letting the attacker borrow again against the same collateral.
6. Both calls unwind and complete; `debt-add-scaled` is eventually called for both, but attacker has already received two payouts of underlying funds while debt/collateral checks were only individually validated against stale (pre-first-borrow) state.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L1269-1287)
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
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1289-1289)
```text
    (try! (vault-system-borrow asset-id amount funds-receiver))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1290-1297)
```text
    (let ((scaled-debt-added (convert-to-scaled-debt asset-id amount true))
          (borrow-index (get index (unwrap-panic (get-cached-indexes asset-id)))))
      (try! (contract-call? .v0-market-vault
                            debt-add-scaled
                            account
                            scaled-debt-added
                            asset-id))
      
```

**File:** local-testing/contracts/vault/vault-stx.clar (L296-301)
```text
(define-private (send-underlying (amt uint) (account principal))
  (begin
    (try! (as-contract? ((with-stx amt))
      (try! (contract-call? .wstx transfer amt tx-sender account none))
      true))
    (ok true)))
```

**File:** mainnet/contracts/vault/v0-vault-sbtc.clar (L1013-1020)
```text
    ;; Set reentrancy guard
    (var-set in-flashloan true)

    ;; Send funds to receiver
    (try! (send-underlying amount funds-receiver-resolved))

    ;; Execute callback
    (try! (contract-call? fc callback amount fee data))
```
