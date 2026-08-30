### Title
Unconditional `insert` in `v0-market-vault.clar` clobbers concurrent registry `mask` updates during nested calls - (File: `mainnet/contracts/market/v0-market-vault.clar`)

### Summary
Every state-mutating public entry point in `v0-market-vault.clar` (`collateral-add`, `collateral-remove`, `debt-add-scaled`, `debt-remove-scaled`) snapshots the account's `registry` entry (`mask`, etc.) at the *start* of its `let`, computes `updated-entry` from that stale snapshot, and performs an unconditional `(insert updated-entry)` at the end without re-reading `registry`/`reverse` immediately before the write. If any nested call for the same account's obligation `id` runs in between (e.g. triggered by a `<ft-trait>` transfer callback during `collateral-add`), the outer call's final `insert` silently overwrites the nested call's `mask` update while leaving the nested call's `collateral`/`debt` map entries intact, desynchronizing `mask` from the actual `collateral`/`debt` maps.

### Finding Description
`resolve-or-create`/`resolve` read the obligation once via `map-get?` [1](#0-0)  and each public function binds `entry`, `mask`, `updated-mask`, and `updated-entry` in the *same* `let` before any external interaction happens, e.g. in `collateral-add`: [2](#0-1) 

Note that `receive-tokens` (a dynamic-dispatch `contract-call?` on the attacker-suppliable `<ft-trait>` argument) executes at line 387, strictly *before* `(insert updated-entry)` at line 389. `receive-tokens` itself is a plain trait call with no reentrancy guard: [3](#0-2) 

`insert` performs a raw `map-set` on both `registry` and `reverse` with whatever `params` it was given, with no re-read/compare-and-swap against the current on-chain state: [4](#0-3) 

The identical pattern (snapshot `mask` → mutate collateral/debt map → unconditional `insert updated-entry`, with no re-read right before the write) repeats in `collateral-remove`, `debt-add-scaled`, and `debt-remove-scaled`: [5](#0-4) [6](#0-5) [7](#0-6) 

Access is nominally restricted via `check-impl-auth`, which only checks that the *immediate* `contract-caller` equals the registered `impl` principal (the active market implementation, e.g. `.v0-4-market`): [8](#0-7) 

This does **not** block cross-function reentrancy through the implementation contract itself: since the attacker is explicitly permitted to "deploy its own Clarity contract and pass it as `<ft-trait>`," a malicious token contract's `transfer` implementation can call back into `.v0-4-market`'s own public entry points (e.g. another `collateral-add`/`debt-add-scaled` call for the same account but a different `asset-id`) *before* returning control to the outer `receive-tokens` call. Because that reentrant call is routed back through `.v0-4-market` (the registered `impl`), `check-impl-auth` still passes for the nested vault call. The nested call fully completes: it writes its own `collateral`/`debt` map entry and calls `insert` with a mask that correctly includes its own new bit (computed from the *pre-outer* mask, since the outer hasn't inserted yet). Control then returns to the outer `collateral-add`, which resumes from its already-bound `updated-entry` (computed before the reentrant call happened) and performs its own `insert`, overwriting `registry`'s `mask` field back to a value that no longer includes the bit the nested call just set — even though the nested call's `collateral`/`debt` map entry remains written.

No code path re-validates or merges `mask` against current storage before the final `insert`; there is no compare-and-swap, no version counter, and no reentrancy lock anywhere in this contract.

### Impact Explanation
This is a read-modify-write race on the `mask` field of the `registry` map that any two nested operations on the same obligation `id` can trigger. The desynchronized `mask` causes `lookup-collateral`/`lookup-debt` (used by `get-position`, which is used for health/liquidation accounting in `.v0-4-market`) to silently omit collateral or debt that is actually present in the `collateral`/`debt` maps. This can hide real debt from health checks (enabling under-collateralized borrowing invisible to liquidation) or hide real collateral (breaking accounting), both of which constitute **protocol insolvency** — a Critical-severity impact per the scope's Critical category ("permanent freezing of funds, or protocol insolvency").

### Likelihood Explanation
Preconditions: the attacker must be able to (1) supply their own contract as the `<ft-trait>` argument to a collateral operation, and (2) have that operation reenter another vault-mutating call for the same account before the outer call's `insert`. The task's rules explicitly grant capability (1) to the attacker ("deploys its own Clarity contract and passes it as `<ft-trait>`"). Capability (2) depends on whether `.v0-4-market` (the `impl`) validates the supplied `ft` principal against the DAO-registered asset address before calling into the vault, and whether `.v0-4-market`'s own public functions have reentrancy guards — I was not able to fully confirm this from the truncated portion of `v0-4-market.clar` available to me (the file is 1661 lines; only the first 1000 were retrieved, and the public deposit/borrow entry points that pass `<ft-trait>` through were not shown). If such validation is absent or bypassable, this is a zero-capital, fully repeatable attack triggerable in a single transaction/block by any unprivileged principal. The structural defect in `v0-market-vault.clar` itself (unconditional `insert` from a stale snapshot, no re-read before write) is confirmed directly from the code shown above regardless of that open question.

### Recommendation
Make the registry read-modify-write atomic with respect to nested calls: immediately before each `insert`, re-read the current `registry` entry for the same `id` and merge only the intended delta (e.g., OR in / AND-NOT the specific bit being toggled) rather than writing a `mask` value computed from a stale snapshot taken before any external call. Alternatively, add a simple reentrancy guard (e.g., a `in-progress` data-var keyed by `id`) around all four public mutators, and/or reorder `collateral-add` so all external `<ft-trait>` calls happen strictly after the final `insert` (checks-effects-interactions pattern).

### Proof of Concept
Clarinet/vitest simnet plan:
1. Deploy a malicious FT contract implementing `ft-trait` whose `transfer` function, when called with a specific amount/sentinel, calls back into `.v0-4-market`'s collateral-deposit entry point (or directly `.v0-market-vault` `collateral-add` if `check-impl-auth` can be satisfied in the test harness by setting `impl` to the test's calling contract) for the *same account* but a *different* `asset-id`.
2. Register this malicious FT as a usable collateral asset in `v0-assets` (or bypass by calling `v0-market-vault` directly with `tx-sender` set as `impl` via `set-impl` in the test setup, isolating the vault-level bug from the `v0-4-market` validation question).
3. Call `collateral-add(account, amount, malicious-ft, asset-id=A)`.
4. Inside the malicious `transfer`, trigger a nested `collateral-add(account, amount2, real-ft, asset-id=B)` which completes fully (map-set `collateral {id, B}`, and `insert` with mask bit B set).
5. Assert: after the outer call returns, `map-get? collateral {id, asset: B}` still shows the nested deposit amount, but `(lookup id)`'s `mask` field does **not** have bit B set (only bit A, from the outer's stale `updated-entry`) — proving the outer `insert` clobbered the nested mask update while leaving the nested collateral write intact.
6. Additionally assert `get-position` / `lookup-collateral` for the account omits asset B's balance from enumeration despite the nonzero `collateral` map entry, demonstrating the accounting desync that leads to insolvency-relevant misreporting.

### Citations

**File:** mainnet/contracts/market/v0-market-vault.clar (L132-133)
```text
(define-private (check-impl-auth)
  (ok (asserts! (is-eq contract-caller (var-get impl)) ERR-AUTH)))
```

**File:** mainnet/contracts/market/v0-market-vault.clar (L143-148)
```text
(define-private (resolve-or-create (account principal))
  (let ((id? (map-get? reverse account)))
    (match id?
      id (lookup id)
         (create account)
    )))
```

**File:** mainnet/contracts/market/v0-market-vault.clar (L159-169)
```text
(define-private (insert (params
                        {
                          id: uint,
                          account: principal,
                          mask: uint,
                          last-update: uint,
                          last-borrow-block: uint,
                        }))
  (let ((id (get id params)))
    (map-set registry id params)
    (map-set reverse (get account params) id)))
```

**File:** mainnet/contracts/market/v0-market-vault.clar (L256-257)
```text
(define-private (receive-tokens (asset <ft-trait>) (amount uint) (account principal))
  (contract-call? asset transfer amount account current-contract none))
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

**File:** mainnet/contracts/market/v0-market-vault.clar (L406-438)
```text
(define-public (collateral-remove (account principal) (amount uint) (ft <ft-trait>) (asset-id uint) (recipient principal))
  (let ((states (var-get pause-states))
        (entry (resolve account))
        (user-id (get id entry))
        (mask (get mask entry))
        (remaining (try! (remove-user-collateral user-id asset-id amount)))
        (updated-mask (if (is-eq remaining u0)
                        (mask-update mask asset-id true false) ;; collateral, remove
                        mask))
        (updated-entry (merge entry (refresh updated-mask))))

    (try! (check-impl-auth))
    (asserts! (not (get collateral-remove states)) ERR-PAUSED)
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)

    (insert updated-entry)
    (try! (send-tokens ft amount recipient))
    
    (print {
      action: "collateral-remove",
      caller: contract-caller,
      data: {
        account: account,
        recipient: recipient,
        asset-id: asset-id,
        amount: amount,
        updated-collateral-amount: remaining,
        mask-before: mask,
        mask-after: updated-mask
      }
    })
    
    (ok remaining)))
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

**File:** mainnet/contracts/market/v0-market-vault.clar (L473-503)
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

    (insert updated-entry)
    
    (print {
      action: "debt-remove-scaled",
      caller: contract-caller,
      data: {
        account: account,
        asset-id: asset-id,
        scaled-amount: scaled-amount,
        updated-scaled-debt: remaining,
        mask-before: mask,
        mask-after: nmask
      }
    })
    
    (ok remaining)))
```
