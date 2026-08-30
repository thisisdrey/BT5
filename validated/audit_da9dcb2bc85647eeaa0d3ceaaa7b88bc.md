### Title
Stale `let`-captured obligation entry in `collateral-add`/`collateral-remove` overwrites concurrently-updated `mask` on reentrancy, desynchronizing `mask` from actual nonzero `debt`/`collateral` map rows - (File: `mainnet/contracts/market/v0-market-vault.clar`)

### Summary
`collateral-add` and `collateral-remove` in `v0-market-vault.clar` compute `updated-entry` (including the new `mask`) in a `let` binding at function entry, then perform an external token transfer (`receive-tokens`/`send-tokens`) mid-function, and only write that stale `updated-entry` to the `registry`/`reverse` maps via `insert` after the transfer returns. Because the external call target is an attacker-supplied `<ft-trait>`, the attacker can reenter and perform a `debt-add-scaled`/`debt-remove-scaled` (or a second collateral op) on the same account for a different asset before the outer call's `insert` executes, causing the outer call's stale mask to overwrite (silently discard) the mask bit set by the reentrant call, even though the reentrant call's `debt`/`collateral` map row was already durably committed.

### Finding Description
`collateral-add` (mainnet/contracts/market/v0-market-vault.clar:374-404) captures `entry`, `mask`, `updated-mask`, and `updated-entry` in its `let` bindings, and also executes `(add-user-collateral user-id asset-id amount)` as part of the same `let` evaluation — i.e., the `collateral` map row is mutated *before* `receive-tokens` is even invoked [1](#0-0) . `receive-tokens` then calls `(contract-call? asset transfer amount account current-contract none)` where `asset` is the attacker-supplied `<ft-trait>` principal [2](#0-1) . Only after this external call returns does the function call `(insert updated-entry)` (line 389), writing the `let`-captured (now stale) `mask` value into `registry`/`reverse`.

`check-impl-auth` only asserts `contract-caller == (var-get impl)` (i.e., the immediate caller is the `market` contract) [3](#0-2) ; it does not prevent nested calls through `market.clar` during the same transaction, since `contract-caller` in Clarity reflects only the direct caller of each call frame, not the top-level `tx-sender`, and any reentrant call routed back through `market.clar`'s public entry points (e.g. `borrow`) still satisfies this check when it reaches `v0-market-vault`.

Exploit flow:
1. Attacker deploys a malicious `<ft-trait>` contract whose `transfer` function, instead of performing a normal transfer, calls back into `market.clar`'s `borrow` (or another debt/collateral function) for the same `account`.
2. Attacker calls the `market.clar` supply/collateral-add entry point with `ft` = the malicious contract for asset Y. This reaches `v0-market-vault.collateral-add`, which: captures `entry`/`mask0`, computes `updated-mask = mask0 | bit(Y)`, credits the `collateral` map for Y, then calls `receive-tokens`.
3. Inside the malicious `transfer`, the attacker reenters `market.clar`'s `borrow` for debt asset X on the same account. This reaches `v0-market-vault.debt-add-scaled`, which reads the registry (`mask0`, unchanged since the outer `insert` hasn't happened yet), computes `mask0 | bit(X)`, writes `debt` map row for X, and commits `insert` with `mask0 | bit(X)` — this fully completes before returning control to the outer call.
4. Control returns to the outer `collateral-add`. It calls `(insert updated-entry)` with its own stale `updated-entry` containing `mask0 | bit(Y)` (captured in step 2, before the reentrant write), overwriting the registry and erasing bit(X) that was just committed.
5. Final state: `debt` map has a nonzero row for asset X (`get-debt id X` != empty, `add-user-scaled-debt` succeeded and was never rolled back), but `registry` mask lacks bit(X). `get-position`'s `lookup-debt` (mainnet/contracts/market/v0-market-vault.clar:317-333) iterates only bits set in `mask`, so asset X's debt is permanently invisible to `get-position`, `market.clar`'s health checks, LTV/egroup evaluation, and liquidation enumeration (`get-full-position`/`get-liquidation-position` in `v0-4-market.clar:466-475`), all of which rely on the same mask-driven enumeration.

Existing checks (`check-impl-auth`, pause states, `> amount u0`, health checks in `market.clar`) do not stop this because: (a) `check-impl-auth` is satisfied by any call routed through `market.clar`, including reentrant nested calls; (b) health checks in `borrow`/`repay` operate on `get-position`, which is exactly the corrupted read this bug produces, so the check is evaluated against an incomplete debt/collateral view rather than being a defense against it.

### Impact Explanation
This is Critical: protocol insolvency. Debt can be created (and its underlying tokens borrowed out via `vault-system-borrow`) while being permanently excluded from the mask-driven `get-position` enumeration used for all subsequent health checks, borrow/repay accounting, and liquidation. The debt is real (backed by a nonzero `debt` map row and actual disbursed tokens) but uncollateralized-checked and unliquidatable through the normal mask-based liquidation path, since `liquidate` also relies on `get-liquidation-position`/`get-full-position`, which enumerate via the same corrupted mask. This is unbacked, unrecoverable debt exposure directly threatening protocol solvency.

### Likelihood Explanation
Preconditions: the attacker needs to (1) deploy their own `<ft-trait>` implementation and pass it into a `market.clar` collateral operation, and (2) have that contract's `transfer` function reenter `market.clar`'s debt operation for the same account during the callback window. This requires no privileged role, no oracle/price control beyond what any user already supplies, and no DAO access — only ordinary user capabilities (deploy contract, call public functions, control call ordering), all explicitly in scope. Capital cost is limited to the collateral amount needed to open the window and enough legitimate debt capacity to make the desynchronized debt meaningful; the attack is repeatable across assets/accounts.

### Recommendation
Re-read the obligation's current `mask` (or, more robustly, apply the mask delta transactionally) immediately before the final `insert`, rather than relying on the `let`-captured snapshot taken before the external token transfer. Equivalently, perform all external calls (`receive-tokens`/`send-tokens`) strictly before any local state mutation of `collateral`/`debt`/`mask`, or add a reentrancy guard (a `bool` lock variable checked/set at function entry and cleared at exit) shared across `collateral-add`, `collateral-remove`, `debt-add-scaled`, and `debt-remove-scaled` so a nested call for the same or different asset cannot execute until the outer call fully commits its state.

### Proof of Concept
Clarinet/vitest simnet test plan (local fork):
1. Deploy a malicious `ft-trait` mock contract `evil-ft` whose `transfer` function, when called with a specific trigger amount, issues a nested `contract-call?` into `market.clar`'s `borrow` function for debt asset X on the same `tx-sender`.
2. Register asset X (debt-enabled) and asset Y (collateral-enabled) in `.v0-assets`/`.v0-egroup` with permissive LTV so the reentrant `borrow` passes health checks against the incomplete position.
3. As attacker principal, call `market.clar`'s supply/collateral-add entry point with `ft = evil-ft`, `asset-id = Y`, amount sufficient to trigger the reentrant callback.
4. Inside the callback, have `evil-ft` call `market.clar borrow` for asset X with a modest amount to `contract-caller` (attacker).
5. After the outer call returns `(ok ...)`, assert:
   - `(contract-call? .v0-market-vault get-debt user-id X)` returns a nonzero `scaled` value.
   - `(contract-call? .v0-market-vault get-position attacker MAX-U128)` returns a `debt` list that does NOT contain an entry with `aid: X`.
   - This demonstrates the divergence: `get-debt` shows debt for X, but `get-position`'s enumerated `debt` list omits it, proving the mask/`debt`-map desync and the resulting invisible, unliquidatable debt.

### Citations

**File:** mainnet/contracts/market/v0-market-vault.clar (L132-133)
```text
(define-private (check-impl-auth)
  (ok (asserts! (is-eq contract-caller (var-get impl)) ERR-AUTH)))
```

**File:** mainnet/contracts/market/v0-market-vault.clar (L256-257)
```text
(define-private (receive-tokens (asset <ft-trait>) (amount uint) (account principal))
  (contract-call? asset transfer amount account current-contract none))
```

**File:** mainnet/contracts/market/v0-market-vault.clar (L374-389)
```text
(define-public (collateral-add (account principal) (amount uint) (ft <ft-trait>) (asset-id uint))
  (let ((states (var-get pause-states))
        (entry (resolve-or-create account))
        (user-id (get id entry))
        (mask (get mask entry))
        (updated-mask (mask-update mask asset-id true true)) ;; collateral, insert
        (updated-entry (merge entry (refresh updated-mask)))
        (result (add-user-collateral user-id asset-id amount)))

    (try! (check-impl-auth))
    (asserts! (not (get collateral-add states)) ERR-PAUSED)
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)

    (try! (receive-tokens ft amount account))
    
    (insert updated-entry)
```
