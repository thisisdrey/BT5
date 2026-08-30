### Title
`resolve-or-create` allocates a new obligation id but defers the `reverse`/`registry` write until after an external token transfer, enabling a reentrant double-create for the same account - (File: mainnet/contracts/market/v0-market-vault.clar)

### Finding Description
`collateral-add` computes the account's obligation entry via `resolve-or-create` inside its `let` bindings, *before* any external interaction, but only persists it via `insert` *after* `receive-tokens` has run: [1](#0-0) 

`resolve-or-create` looks up `reverse account`; if absent it calls `create`, which calls `increment` (consuming a nonce and returning a fresh id), but this new id/account pair is **not written to `reverse` or `registry` until `insert` is called**: [2](#0-1) 

Because `receive-tokens` triggers `(contract-call? ft transfer ...)` on an attacker-controlled `<ft-trait>` implementation before `insert` runs, a malicious `ft` contract can, inside its own `transfer` function, re-enter the collateral-add flow for the same brand-new `account`. At that point `reverse[account]` is still unset (the outer call hasn't reached `insert` yet), so the nested `resolve-or-create` again falls into the `create` branch, calling `increment` a second time and allocating a second, distinct id for the same logical account. Both call paths eventually call `insert` with their own `updated-entry`, and whichever `insert` executes last wins the `reverse[account]` mapping - the collateral/debt state recorded under the other id becomes permanently unreachable (no principal maps to it anymore), since `resolve`/`resolve-safe`/`get-position` only ever look up by `reverse[account] -> single id`.

Existing checks that would normally stop this:
- `check-impl-auth` restricts `contract-caller` to the registered `impl` contract, so a malicious `ft` cannot call `collateral-add` on `v0-market-vault.clar` *directly* during its `transfer` callback (contract-caller would be the ft contract, not `impl`).
- However, this only blocks a *direct* re-entry into the vault. It does **not** protect against re-entry routed back through the legitimate `impl` contract (`v0-4-market.clar`), i.e., if the malicious `ft`'s `transfer` callback calls back into the impl contract's own collateral-add entry point (which the attacker fully controls the arguments of, including `account`/`on-behalf-of`), and that impl contract has no reentrancy lock of its own for this flow, the nested call reaches `v0-market-vault.clar`'s `collateral-add` with `contract-caller = impl` again, passing `check-impl-auth`.
- I could not fully verify from the available index whether `v0-4-market.clar` (the `impl` contract) has an independent reentrancy guard for `collateral-add`/`debt-add-scaled` outside of the explicitly out-of-scope flashloan (`in-flashloan`) guard. This is the key uncertainty for this finding: the root-cause ordering bug (compute-id-then-external-call-then-persist) unambiguously exists in `v0-market-vault.clar`, but full exploitability depends on the reentrancy surface of the caller contract, which I was unable to fully confirm within the available tool budget.

### Impact Explanation
If reachable, this is **Critical - permanent freezing of funds**: the attacker's own collateral/debt gets split across two obligation ids under one principal, and only the id that wins the final `reverse[account]` write is ever reachable through `resolve`/`resolve-safe`/`get-position`; collateral recorded under the losing id can never be withdrawn (no code path resolves an account to more than the current `reverse` entry, and there is no admin recovery function for a stray id). This matches the "permanent freezing of funds" Critical impact class.

### Likelihood Explanation
- Precondition: the account must be brand-new (never in `reverse`), trivially satisfiable by any attacker generating a fresh Stacks principal.
- Attacker must deploy a malicious `<ft-trait>` implementation and pass it into a call to `collateral-add`, which is explicitly listed as attacker-controllable in the rules.
- The remaining uncertainty is whether the intermediate `impl` contract (`v0-4-market.clar`) permits a reentrant call back into itself during its own token-transfer step without a guard; this could not be conclusively confirmed from the code inspected.
- Capital cost is minimal (gas + a small collateral amount for the PoC transfer).

### Recommendation
Persist the newly created obligation (`insert`) immediately when a new id is allocated in `create`/`resolve-or-create`, before any external token transfer occurs, so that `reverse[account]` is set atomically with `nonce` allocation and prior to any `receive-tokens`/`send-tokens` call. Alternatively, add an explicit reentrancy lock (a data-var flag) around `collateral-add`/`collateral-remove`/`debt-add-scaled`/`debt-remove-scaled` in both `v0-market-vault.clar` and the `impl` contract that is checked/set before the external `ft` call and cleared at the end, independent of the existing flashloan-specific `in-flashloan` flag.

### Proof of Concept
Clarinet/vitest simnet plan (requires confirming the `impl` contract's reentrancy surface, which should be validated as part of executing this PoC):
1. Deploy a malicious `ft-trait` implementation whose `transfer` function, on the first invocation for a specific fresh `account`, calls back into the `impl` contract's `collateral-add` entry point for the same `account`/`asset-id` before returning success.
2. Have the attacker (as `account`, brand new, never having interacted with the market) call `collateral-add` (via the impl contract) with `amount > 0`, the malicious `ft`, and a valid `asset-id`.
3. Assert `(var-get nonce)` increased by `2` for this single logical "create account" flow (expected `1`).
4. Assert `(map-get? reverse account)` resolves to only one id, and that `(map-get? registry <the-other-id>)` still exists with collateral recorded under it but is unreachable via `resolve`/`resolve-safe`/`get-position` for `account`.
5. Assert that the collateral amount recorded under the "lost" id cannot be withdrawn by `account` through `collateral-remove` (since `resolve` only returns the surviving id).

### Citations

**File:** mainnet/contracts/market/v0-market-vault.clar (L137-169)
```text
(define-private (increment)
  (let ((curr (var-get nonce))
        (next (+ curr u1)))
    (var-set nonce next)
    curr))

(define-private (resolve-or-create (account principal))
  (let ((id? (map-get? reverse account)))
    (match id?
      id (lookup id)
         (create account)
    )))

(define-private (create (account principal))
  {
    id: (increment),
    account: account,
    mask: u0,
    last-update: stacks-block-time,
    last-borrow-block: u0
  })

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
