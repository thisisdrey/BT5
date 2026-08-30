No reentrancy guard exists at the vault or market level. Let me confirm `receive-tokens`, `check-impl-auth`, and `insert` implementations to fully verify the read-after-external-call pattern.### Title
Reentrancy through malicious `<ft-trait>` `transfer` callback in `collateral-add` causes the outer call's stale `updated-entry` to overwrite the inner call's committed mask, permanently freezing the second collateral asset - ([File: mainnet/contracts/market/v0-market-vault.clar])

### Summary
`v0-market-vault.clar`'s `collateral-add` computes `updated-entry` (with the new mask bit) from the registry state read *before* the external token transfer, then calls `receive-tokens` (which invokes the attacker-controlled `ft.transfer`) before finally calling `insert updated-entry`. Because `insert` is a plain `map-set` with no optimistic-lock/staleness check, a reentrant call to `collateral-add` for a different `asset-id` that fully completes (map-set to `collateral` and `insert` to `registry`) during the outer call's `transfer` will have its mask update silently overwritten when the outer call resumes and performs its own `insert` with the older `updated-entry` value.

### Finding Description
The relevant code path is:
- `mainnet/contracts/market/v0-4-market.clar` `collateral-add` (lines 1020-1104) calls `contract-call? .v0-market-vault collateral-add account amount ft asset-id`. [1](#0-0) 
- `v0-market-vault.clar` `collateral-add` (lines 374-404) reads `entry`, computes `mask`, `updated-mask`, and `updated-entry` all in the `let` bindings evaluated up front, then calls `(try! (receive-tokens ft amount account))` — this triggers `(contract-call? asset transfer amount account current-contract none)` — **before** calling `(insert updated-entry)`. [2](#0-1) 
- `receive-tokens` is a direct pass-through `contract-call?` to the attacker-supplied `ft` trait implementation's `transfer` method, giving the attacker principal full control of code execution at that point: [3](#0-2) 
- `insert` is an unconditional `map-set` with no version/nonce check against concurrent modification: [4](#0-3) 

Exploit flow: attacker calls `market.collateral-add(ft1, amount1, feeds)` for `asset1`. Inside, `v0-market-vault.collateral-add` computes `updated-entry₁ = mask(asset1)` from the pre-call mask (`u0`), then calls `receive-tokens ft1 ...`, which calls the attacker's malicious `ft1.transfer`. That callback re-enters `market.collateral-add(ft2, amount2, feeds)` for `asset2` for the *same account*. This nested call re-reads the registry (still showing mask `u0`, since the outer call hasn't inserted yet), computes `updated-entry₂ = mask(asset2)`, calls `add-user-collateral` (which `map-set`s the `collateral` map for `asset2` — this succeeds and is durable), then calls `insert updated-entry₂` (mask = bit(asset2)) — this commits successfully because nothing prevents nested calls to `insert`. The inner call returns `ok`, `ft2.transfer` returns to the outer `ft1.transfer`, and control returns to the outer `v0-market-vault.collateral-add`, which then executes `(insert updated-entry₁)` with the **stale** `updated-entry₁` computed at the start of the *outer* call (mask = bit(asset1) only, since it was computed before the reentrant `asset2` insert happened). This unconditionally `map-set`s the registry entry, wiping out the `asset2` bit that was just committed.

End state: `collateral` map still has a nonzero amount for `{id, asset2}` (written by `add-user-collateral` in the inner call and never touched again), but `registry`'s `mask` field no longer has the `asset2` bit set. Every function that determines "which collateral assets a user holds" (health checks, liquidation eligibility, `get-position`/`get-full-position`, `lookup-collateral` via `mask-to-list-collateral`) relies exclusively on the `mask` field to enumerate relevant assets, so `asset2`'s collateral becomes permanently invisible to the protocol while still being locked in the vault's token balance — it cannot be withdrawn (removal path also depends on mask/position enumeration and health checks) and cannot be counted toward collateral value.

There is no `check-caller-auth`/reentrancy guard preventing nested calls to `v0-market-vault.collateral-add`: `check-impl-auth` only checks that `contract-caller` is the current `impl` (i.e., `v0-4-market`), which remains true for both the outer and the reentrant inner call since both originate from `tx-sender`'s call into `market.clar` → `v0-market-vault.clar`. There is also no pause or single-call-per-block mechanism blocking this. `is-eq contract-caller tx-sender` in `market.clar`'s `collateral-add` also does not prevent this, since both calls are still driven synchronously by the same `tx-sender` within the transfer callback chain.

### Impact Explanation
This is a **Critical** finding under "permanent freezing of funds": the attacker's own `asset2` collateral becomes permanently untracked by the mask-based position system after the attack, meaning it can never be withdrawn via `collateral-remove` (which requires the mask to know the asset is present to run correct health/removal logic) and is excluded from every health/liquidation calculation. While the tokens remain physically held by the vault contract, they are functionally frozen and orphaned from the user's tracked position — an accounting break that also skews global protocol solvency assumptions (liquidators cannot see or seize this collateral, and the user cannot recover it through normal means).

### Likelihood Explanation
- Preconditions require only an ordinary attacker: fund a wallet with two token balances, deploy one malicious FT-trait contract that reenters on `transfer`, and have the asset registered (which is DAO controlled — the attacker doesn't need to register a fully "legit" asset, only needs `asset1`/`asset2`/`ft2` to pass `get-asset`/`get-egroup` checks in `market.clar`; using two already-DAO-approved assets like zSTX/zsBTC as `asset1`/`asset2` and only making `ft1`'s FT contract malicious is sufficient, since it's `ft1.transfer` that needs to be attacker-controlled).
- No privileged role, oracle manipulation, or DAO compromise needed — the attacker fully controls call ordering via the malicious contract they deploy and pass as `<ft-trait>`.
- Fully repeatable: the attacker can perform this attack in a single transaction/block, and it can be done for any pair of distinct assets, potentially chained to freeze multiple assets in a single position.

### Recommendation
Re-read the registry mask immediately before `insert`, or use a check-effects-interactions pattern: perform `receive-tokens` (external call) strictly before any read of mutable state used to compute the final entry, and recompute `updated-entry` (or at minimum the `mask` field) from the freshest on-chain `registry` state right before `insert`, rather than from values captured earlier in the same `let`. Alternatively, add a reentrancy guard (a `bool` data-var toggled at function entry/exit) around `v0-market-vault.collateral-add`/`collateral-remove`/`debt-add-scaled`/`debt-remove-scaled` to reject nested calls into the vault for the same or any account during an in-flight token transfer.

### Proof of Concept
Clarinet simnet test outline (vitest/Clarinet):
1. Deploy a malicious FT contract `malicious-ft.clar` implementing `<ft-trait>` whose `transfer` method, on the first invocation only (guarded by a data-var flag), calls `(contract-call? .v0-4-market collateral-add .legit-ft2 amount2 none)` for `asset2`, then performs the normal SIP-010 balance transfer/mint logic to satisfy the outer call's expectations, and returns `(ok true)`.
2. Register `malicious-ft` as `asset1` and a normal `legit-ft2` as `asset2` via DAO test helpers (existing pattern in test setup).
3. Fund attacker principal with `asset1` and `asset2` balances; ensure the attacker starts with an empty/zero-collateral position (`(unwrap-panic (map-get? reverse account))` absent).
4. Call `market.collateral-add(malicious-ft, amount1, none)` as the attacker (`tx-sender`).
5. Assert:
   - `v0-market-vault.get-collateral(user-id, asset2)` returns `amount2 > 0` (the token accounting for asset2 exists).
   - `v0-market-vault.lookup(user-id).mask` does NOT have the `asset2` bit set (only `asset1`'s bit is present), proving the mask was overwritten by the outer call's stale `insert`.
   - Subsequent call to `market.collateral-remove(legit-ft2, amount2, none, none)` fails or the position/health check computed via `get-position`/`get-full-position` never includes `asset2`, confirming the collateral is unreachable through normal protocol flows despite existing in the `collateral` map.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L1089-1091)
```text
    ;; Execute collateral add (existing logic)
    (let ((result (try! (contract-call? .v0-market-vault collateral-add account amount ft asset-id))))
      
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
