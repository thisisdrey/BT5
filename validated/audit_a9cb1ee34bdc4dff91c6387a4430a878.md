### Title
Read-before-write ordering in `collateral-add` lets a malicious FT re-enter `debt-add-scaled`, causing debt to be recorded with a mask bit that the outer call overwrites and clears - ([File: mainnet/contracts/market/v0-market-vault.clar])

### Summary
`collateral-add` computes the account's updated obligation-registry entry (mask, timestamps) from a `let`-bound snapshot taken *before* it calls `receive-tokens`, and only persists that snapshot via `insert` *after* the token transfer completes. Because `receive-tokens` invokes an attacker-supplied `<ft-trait>` contract's `transfer` function, the attacker can re-enter the protocol (via the market's borrow entry point, which calls `debt-add-scaled`) using the same stale registry snapshot, causing the debt bit set by the nested call to be silently overwritten and cleared when the outer `collateral-add` finally commits.

### Finding Description
In `collateral-add`:
```
(define-public (collateral-add (account principal) (amount uint) (ft <ft-trait>) (asset-id uint))
  (let ((states (var-get pause-states))
        (entry (resolve-or-create account))
        (user-id (get id entry))
        (mask (get mask entry))
        (updated-mask (mask-update mask asset-id true true))
        (updated-entry (merge entry (refresh updated-mask)))
        ...)
    ...
    (try! (receive-tokens ft amount account))
    (insert updated-entry)
    ...))
``` [1](#0-0) 

`entry`/`mask`/`updated-mask`/`updated-entry` are all computed **before** `receive-tokens` runs, and the registry write (`insert`) happens **only after** the FT transfer completes. `receive-tokens` performs `(contract-call? asset transfer amount account current-contract none)` on the attacker-controlled `ft` trait argument. [2](#0-1) 

Since the attacker deploys the FT contract themselves, its `transfer` implementation can call back into the market contract's borrow entry point during this window (before the outer `insert` for the collateral-add has run). That nested call flows into `debt-add-scaled`, which independently calls `resolve-or-create`/reads `(get mask entry)` on the same account and computes its own `update-mask` from the *same still-stale* registry row (mask `m0`, no collateral bit yet, no debt bit yet). It sets the debt bit, writes the debt map row, and commits via `insert` — all before the outer `collateral-add` resumes:
```
(define-public (debt-add-scaled (account principal) (scaled-amount uint) (asset-id uint))
  (let ((entry (resolve-or-create account))
        (mask (get mask entry))
        (update-mask (mask-update mask asset-id false true))
        (updated-entry (merge entry { mask: update-mask, ... }))
        (result (add-user-scaled-debt user-id asset-id scaled-amount)))
    ...
    (insert updated-entry)
    ...))
``` [3](#0-2) 

When execution returns to the outer `collateral-add`, it resumes at `(insert updated-entry)` using the `updated-entry` value captured *before* the reentrant call — i.e., `mask = m0 | collateralBit`, which does not include the debt bit the nested call just committed. This `insert` overwrites `registry[user-id]`, silently erasing the debt bit even though `debt-scaled(user-id, debtAsset) > 0` remains in the `debt` map.

The `check-impl-auth` assertion in `debt-add-scaled` (`(is-eq contract-caller (var-get impl))`) does not block this, because the immediate `contract-caller` seen by the vault is still the market implementation contract itself (the market contract calls the vault directly during the nested borrow), regardless of the fact that the outer call chain originated from the malicious FT's `transfer` hook. [4](#0-3) 

No reentrancy guard/mutex exists in the vault or in the portion of `v0-4-market.clar` reviewed to prevent a nested call into `debt-add-scaled` while a `collateral-add` invocation is mid-flight.

The invariant that a set mask bit is the sole indicator of a non-zero row is broken: `lookup-debt`/`get-position` iterate only over bits set in `mask` (via `mask-to-list-debt`) [5](#0-4) , so a debt row with a cleared mask bit is invisible to health checks and to liquidation logic that relies on `get-position`.

### Impact Explanation
This is a Critical / protocol insolvency finding: the attacker's account ends up with real, non-zero debt (recorded in the `debt` map) that is completely invisible to every consumer of `mask` (`get-position`, `lookup-debt`, health-factor and liquidation checks). The account will appear fully healthy/over-collateralized and can never be liquidated for this hidden debt, while the attacker has effectively borrowed protocol funds for free with no way for the protocol to recover them — a direct insolvency vector.

### Likelihood Explanation
Preconditions are all attacker-controlled and cheap: the attacker needs (1) an already-tracked account id with some existing collateral (trivial, one prior deposit), (2) a self-deployed FT contract implementing the `ft-trait` with a malicious `transfer` function, and (3) sufficient collateral of a *different* asset to pass the nested borrow's health check at the point of reentry. No privileged role, oracle manipulation, or DAO action is required — only ordinary calls the attacker fully controls (their own token contract passed as `<ft-trait>`, and normal `supply-collateral`/`borrow` market entry points). This is fully repeatable per victim/attacker account.

### Recommendation
Follow checks-effects-interactions: in `collateral-add` (and symmetrically double-check `debt-add-scaled`/`collateral-remove`/`debt-remove-scaled`), perform all state reads/mask computation and the `insert` (registry commit) and `add-user-collateral` map write *before* calling `receive-tokens`/any external contract-call, or re-read the registry entry fresh immediately before the final `insert` rather than relying on a pre-transfer snapshot. Additionally, consider adding an explicit reentrancy guard (a per-account or global "in-progress" flag checked/set/cleared across the vault's mutating entry points) so that a nested call from within `receive-tokens`/`send-tokens` cannot mutate the same account's registry row concurrently.

### Proof of Concept
Clarinet/vitest simnet plan:
1. Deploy a malicious FT contract implementing `ft-trait` whose `transfer` function, when called with a specific marker amount/account, calls back into the market contract's borrow entry point for the same `account` and `debtAsset`, borrowing an amount sized to pass the health check against the attacker's pre-existing collateral of `assetB`.
2. As the attacker, call `supply-collateral`/deposit flow once for `assetB` to create a tracked id `M` with initial collateral (establishes `entry{mask:m0}`).
3. Call the market's supply-collateral entry point for the malicious FT as `assetA`, which routes to vault `collateral-add(account, amountA, malicious-ft, assetA)`.
4. Inside `collateral-add`, after `mask`/`updated-mask` are computed but before `insert`, the injected `receive-tokens` call triggers the malicious FT's `transfer`, which calls the market's `borrow` for `debtAsset`, which calls vault `debt-add-scaled(account, scaledAmt, debtAsset)`.
5. Assert during/after the transaction: `debt-scaled(M, debtAsset) > 0` (debt map row exists) while `(get mask (lookup M))` does NOT have the debt bit set for `debtAsset`.
6. Assert `get-position(account, enabled-mask)`/`lookup-debt` returns no entry for `debtAsset`, i.e., the account appears to have zero debt despite the non-zero `debt` map row, and any health-check/liquidation call using `get-position` reports the account as healthy.

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

**File:** mainnet/contracts/market/v0-market-vault.clar (L317-333)
```text
(define-read-only (lookup-debt (id uint) (mask uint) (enabled-mask uint))
  (let ((init { id: id, result: (list), enabled-mask: enabled-mask })
        (iter (mask-to-list-debt mask))
        (out (fold iter-lookup-debt iter init)))
    (get result out)))

;; -- Position getters -------------------------------------------------------

(define-read-only (get-position (account principal) (enabled-mask uint))
  (match (map-get? reverse account)
    id (let ((obligation (lookup id))
             (user-id (get id obligation))
             (mask (get mask obligation))
             (is-collateral (lookup-collateral user-id mask enabled-mask))
             (is-debt (lookup-debt user-id mask MAX-U128)))
         (ok (merge obligation { collateral: is-collateral, debt: is-debt })))
    (err u600006)))
```

**File:** mainnet/contracts/market/v0-market-vault.clar (L374-404)
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

    (print {
      action: "collateral-add",
      caller: contract-caller,
      data: {
        account: account,
        asset-id: asset-id,
        amount: amount,
        updated-collateral-amount: result,
        mask-before: mask,
        mask-after: updated-mask
      }
    })
      
    (ok result)))
```

**File:** mainnet/contracts/market/v0-market-vault.clar (L442-471)
```text
(define-public (debt-add-scaled (account principal) (scaled-amount uint) (asset-id uint))
  (let ((states (var-get pause-states))
        (entry (resolve-or-create account))
        (user-id (get id entry))
        (mask (get mask entry))
        (update-mask (mask-update mask asset-id false true)) ;; debt, insert
        ;; Oracle frontrunning protection: record current block when borrowing
        (updated-entry (merge entry { mask: update-mask, last-update: stacks-block-time, last-borrow-block: stacks-block-height }))
        (result (add-user-scaled-debt user-id asset-id scaled-amount)))

    (try! (check-impl-auth))
    (asserts! (not (get debt-add states)) ERR-PAUSED)
    (asserts! (> scaled-amount u0) ERR-AMOUNT-ZERO)

    (insert updated-entry)

    (print {
      action: "debt-add-scaled",
      caller: contract-caller,
      data: {
        account: account,
        asset-id: asset-id,
        scaled-amount: scaled-amount,
        updated-scaled-debt: result,
        mask-before: mask,
        mask-after: update-mask
      }
    })
      
    (ok result)))
```
