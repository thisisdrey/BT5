### Title
Unconditional `insert` in `collateral-add` clobbers registry `mask` updated by a reentrant nested call, desynchronizing tracked collateral/debt from the `mask` bitfield - ([File: mainnet/contracts/market/v0-market-vault.clar])

### Summary
All four public state-mutating functions in `v0-market-vault.clar` (`collateral-add`, `collateral-remove`, `debt-add-scaled`, `debt-remove-scaled`) snapshot the account's `registry` entry via `resolve`/`resolve-or-create` at the top of their `let`, then perform an unconditional `(insert updated-entry)` at the end without re-reading the registry immediately before the write. Only `collateral-add` places an external, attacker-influenceable call (`receive-tokens`, i.e. `contract-call? ft transfer ...`) *between* that snapshot and the final `insert`, which lets a nested call on the same account mutate `collateral`/`debt` maps and its own registry `mask` in between - and then be silently overwritten by the outer call's stale `insert`.

### Finding Description
`collateral-add` [1](#0-0)  computes `entry`, `mask`, `updated-mask`, and `updated-entry` from a `resolve-or-create` read taken at the *start* of the `let`, then calls `(try! (receive-tokens ft amount account))` - an external `contract-call?` into the attacker-supplied `<ft-trait>` implementation - and only afterwards performs `(insert updated-entry)`.

`receive-tokens` is `(contract-call? asset transfer amount account current-contract none)` [2](#0-1) . Per the threat model, the attacker may deploy and supply their own contract implementing `ft-trait` as `ft`. Inside that contract's `transfer` function, arbitrary Clarity code can execute, including a call back into the market implementation's own public entrypoint (e.g. `debt-add-scaled`) for the *same account*, which forwards into `v0-market-vault.debt-add-scaled`. Because that inner call still originates from the market-impl contract, it passes `check-impl-auth` (`contract-caller == (var-get impl)`) [3](#0-2) .

The nested `debt-add-scaled` call re-reads the *same stale* `registry` entry (the outer `insert` has not run yet), sets its own debt-bit in `mask`, mutates the `debt` map via `add-user-scaled-debt` [4](#0-3) , and commits its own `insert` with the debt bit set [5](#0-4) . Control then returns up through the token `transfer` call back into the outer `collateral-add`, which resumes and performs `(insert updated-entry)` using its *pre-call* snapshot of `mask` [6](#0-5) . This final write clobbers the nested call's `mask` bit while leaving the nested call's `debt` map entry intact - the account now has real, tracked scaled debt in the `debt` map, but the registry `mask` no longer reflects that a debt position exists for that asset.

No existing check prevents this: `check-impl-auth` only verifies the immediate caller is the market-impl contract (true for both the outer and the reentrant nested call), and there is no mask re-read, no reentrancy guard/mutex, and no `insert` ordering fix (unlike `collateral-remove`, whose `insert` happens *before* its external `send-tokens` call, closing this specific window).

### Impact Explanation
Any code that determines "which assets a position has" by iterating the `mask` field (e.g. `get-position`, `lookup-debt`, `lookup-collateral`, used by health/LTV/liquidation logic in the market implementation) will fail to see the hidden debt because the corresponding bit was dropped from `mask`, even though the actual scaled debt is present and growing in the `debt` map. This lets an attacker create real, uncollateralized/untracked debt that health checks and liquidation logic never enumerate, directly leading to protocol insolvency - this matches the in-scope **Critical** category ("protocol insolvency").

### Likelihood Explanation
The attack requires only: (1) deploying a contract implementing `ft-trait` with a malicious `transfer` function, and (2) calling the market implementation's `collateral-add` wrapper with that contract as `ft`, whose `transfer` callback re-enters the market implementation's `debt-add-scaled` wrapper for the same account. Both capabilities are explicitly listed as available to the attacker in scope. No special privileges, oracle control, or DAO access are needed, and the attack is repeatable per-account and per-transaction, making it low-cost and fully attacker-controlled once the reentrant call path through the market implementation is confirmed reachable (the market-vault side imposes no barrier).

### Recommendation
Re-read (`resolve`/`resolve-or-create`) the registry entry immediately before the final `insert` in every mutating function, and recompute `updated-entry`'s `mask` by applying the intended bit operation to the *freshly read* mask rather than the one captured at function entry - or, simpler, move all registry-affecting `insert` calls to occur *before* any external `contract-call?` (as already done correctly in `collateral-remove`), and add a reentrancy guard (e.g., a per-account or global in-progress flag) around any function that performs an external token call while holding an uncommitted registry mutation.

### Proof of Concept
Clarinet/vitest simnet plan:
1. Deploy a malicious `evil-ft` contract implementing `ft-trait`, whose `transfer` function, when invoked, calls the market implementation's `debt-add-scaled` wrapper for `tx-sender`/`account` with a nonzero `scaled-amount` on `asset-id: B`.
2. As the attacker, call the market implementation's `collateral-add` wrapper with `account = attacker`, `ft = evil-ft`, `asset-id: A`, `amount > 0`.
3. Inside `v0-market-vault.collateral-add`, the outer snapshot of `entry`/`mask` is taken, then `receive-tokens` invokes `evil-ft.transfer`, which reenters and completes `debt-add-scaled` for `asset-id: B` (this nested call succeeds and calls `insert` with mask bit `B` set).
4. Outer `collateral-add` resumes and calls `insert updated-entry` with mask only reflecting bit `A`.
5. Assertions: `(map-get? debt {id: attacker-id, asset: B})` returns the nonzero scaled debt written by the nested call (state mutation persisted); `(get mask (lookup attacker-id))` does **not** have the debt bit for `asset-id: B` set (only the collateral bit for `asset-id: A` is set) - proving mask/debt desynchronization; `lookup-debt` called with the corrupted mask omits asset `B`'s debt entry even though `get-debt` for `(attacker-id, B)` returns the real nonzero balance.

### Citations

**File:** mainnet/contracts/market/v0-market-vault.clar (L132-133)
```text
(define-private (check-impl-auth)
  (ok (asserts! (is-eq contract-caller (var-get impl)) ERR-AUTH)))
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

**File:** mainnet/contracts/market/v0-market-vault.clar (L237-242)
```text
(define-private (add-user-scaled-debt (user-id uint) (asset-id uint) (amount uint))
  (let ((key { id: user-id, asset: asset-id })
        (current-scaled-debt (default-to u0 (get scaled (map-get? debt key)))) ;; graceful default to u0
        (updated-scaled-debt (+ current-scaled-debt amount)))
      (map-set debt key { scaled: updated-scaled-debt })
      updated-scaled-debt))
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
