### Title
Reentrant `collateral-add` calls for a brand-new account can allocate two distinct ids before either is committed, orphaning the first/second id's collateral row - (File: `mainnet/contracts/market/v0-market-vault.clar`)

### Summary
`collateral-add` computes the account's obligation id via `resolve-or-create` → `create` → `increment` and performs `add-user-collateral` (a `map-set` on `collateral`) *before* the untrusted `ft-trait` transfer is invoked, and only calls `insert` (which writes `registry` and, critically, `reverse`) *after* that transfer returns. This is a checks/effects/interactions violation: the account→id binding (`reverse`) is not committed until after control has been handed to attacker-controlled code, so a reentrant `collateral-add` call for the same never-before-seen account allocates a second, different id and commits it to `reverse` first; when the outer call resumes it overwrites `reverse` with its own (earlier) id, permanently orphaning the collateral written under the other id.

### Finding Description
In `v0-market-vault.clar`: [1](#0-0) 

`resolve-or-create` only reads the `reverse` map to decide whether to reuse an existing id; if absent, `create` calls `increment` to mint a brand-new id, but nothing is written to `registry`/`reverse` at that point - that only happens later inside `insert`.

`collateral-add`'s `let` bindings evaluate `resolve-or-create` (and thus `increment` for a new account) and `add-user-collateral` (`map-set collateral`) eagerly, **before** the pause/auth checks and, more importantly, before `receive-tokens` performs the external `ft-trait` `transfer` call. `insert`, which writes `registry` and `reverse`, only runs **after** `receive-tokens` returns: [2](#0-1) 

Exploit flow for an account with no existing `reverse` entry:
1. Attacker calls the public entry point (`market` impl) `collateral-add(account, amount1, malicious-ft, asset1)`, which forwards to `v0-market-vault.collateral-add`. This call's `let`-bindings run `create` → `increment`, allocating `id = N` (nonce becomes `N+1`), and `add-user-collateral(N, asset1, amount1)` writes `collateral{id:N, asset:asset1}`. `reverse` is **not yet** updated.
2. `receive-tokens` invokes `malicious-ft.transfer`, whose attacker-controlled body calls `collateral-add(account, amount2, ft2, asset2)` again for the *same* account, routed through the market impl (so `contract-caller` at the vault is still the impl, satisfying `check-impl-auth`).
3. This nested call's `resolve-or-create` again finds no `reverse[account]` entry (outer hasn't inserted yet), so `create`/`increment` allocates `id = N+1` (nonce becomes `N+2`), `add-user-collateral(N+1, asset2, amount2)` writes `collateral{id:N+1, asset:asset2}`, and this call fully completes and runs `insert`, committing `registry[N+1]` and `reverse[account] = N+1`.
4. Control returns to the outer call, which now runs its own `insert(updated-entry)` with `id = N`, overwriting `registry[N]` and, crucially, `reverse[account] = N` - discarding the inner call's commit.

Result: `reverse[account] = N`; `registry[N+1]` and `collateral{id:N+1, asset:asset2}` still exist in state but are unreachable, because every read/write path (`resolve`, `resolve-safe`, `get-position`, `get-account-scaled-debt`, `collateral-remove`, `debt-*`) resolves the account's current id exclusively through `reverse`. There is no public function that can address id `N+1` for that account again.

No existing check prevents this: `check-impl-auth` only verifies `contract-caller`, which is satisfied because the reentrant call is routed through the legitimate market impl entry point; there is no reentrancy guard, no lock, and no ordering that commits `reverse` before the external transfer.

### Impact Explanation
This is permanent freezing of funds (Critical): the `asset2` collateral committed under the orphaned id `N+1` becomes permanently unreachable by any public read or write function, since all such functions key exclusively off `reverse[account]`. The funds remain locked in the contract with no recovery path.

### Likelihood Explanation
Preconditions are fully within attacker control and require no privileged access: the attacker only needs (a) a fresh account that has never held a position (trivial - any new address), and (b) a self-deployed contract implementing `<ft-trait>` whose `transfer` function reenters the market's `collateral-add` for the same account before returning. The attacker supplies both `ft-trait` implementations and controls call ordering entirely. Capital cost is limited to the two collateral amounts moved (the second of which becomes stuck, so it is a net loss to the attacker unless they use a different beneficiary account to attack, or if they intend to grief another account by front-running its first-ever `collateral-add`, in which case the victim's funds are frozen). This is deterministically repeatable against any account making its very first `collateral-add` call through a malicious-ft-triggered reentrant path.

### Recommendation
Follow checks-effects-interactions strictly in `collateral-add`/`debt-add-scaled` (and the corresponding `-remove` variants): call `insert(updated-entry)` (committing `registry`/`reverse`) immediately after `resolve-or-create`/before any external `contract-call?` to an attacker-supplied `ft-trait`, so that a reentrant call for the same account always sees the already-assigned id via `reverse` and takes the `lookup` branch instead of allocating a new one. Alternatively, add an explicit reentrancy guard (a locked flag keyed by account or globally) that aborts nested `collateral-add`/`debt-add-scaled` calls before the transfer completes.

### Proof of Concept
Clarinet simnet test plan:
1. Deploy a malicious `ft-trait` implementation whose `transfer` function, when invoked with a specific `amount`/`memo` sentinel, calls back into the market impl's `collateral-add` for the same `account` with a second legitimate asset/ft.
2. From a fresh test account with no prior `reverse` entry, call `market.collateral-add(account, amount1, malicious-ft, asset1)`.
3. Assert (via `v0-market-vault.get-nr`) that the nonce advanced by 2 (two ids allocated).
4. Assert `v0-market-vault.get-collateral(N+1, asset2) > 0` (the orphaned collateral row still exists).
5. Assert `v0-market-vault.get-position(account, MAX-U64)` (or `resolve`/`resolve-safe`) shows only `asset1`'s collateral and never lists `asset2`.
6. Assert there is no public function callable by the account or anyone else that can reach id `N+1` again for that account, confirming the `asset2` collateral is permanently unreachable.

### Citations

**File:** mainnet/contracts/market/v0-market-vault.clar (L143-169)
```text
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
