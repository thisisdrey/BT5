### Title
Reentrant `collateral-add` overwrites the registry entry with a stale `last-borrow-block`, silently reverting the oracle-frontrunning protection set by a nested `debt-add-scaled` - ([File: mainnet/contracts/market/v0-market-vault.clar])

### Summary
`collateral-add` captures the account's registry `entry` at the top of its `let` block and derives `updated-entry` from it *before* performing the external token transfer via `receive-tokens`, then calls `insert` only after that external call returns. If the transfer callback triggers a nested `debt-add-scaled` that legitimately sets `last-borrow-block` to the current `stacks-block-height` as an anti-oracle-frontrunning safeguard, the outer `collateral-add`'s subsequent `insert` overwrites the registry with the pre-callback, stale `entry`, silently reverting `last-borrow-block` to its old value and erasing the debt mask bit the nested call set.

### Finding Description
`collateral-add` is defined as: [1](#0-0) 

The key ordering issue: `entry`, `updated-mask`, and `updated-entry` are computed in the `let` bindings, which in Clarity are evaluated before the body executes: [2](#0-1) 

`updated-entry` is built via `merge entry (refresh updated-mask)`, and `refresh` only overrides `mask` and `last-update`, leaving `last-borrow-block` sourced from the original `entry` snapshot: [3](#0-2) 

The external call `(try! (receive-tokens ft amount account))` (a `contract-call?` to the attacker-supplied `<ft-trait>` implementation) happens *after* those bindings, and `(insert updated-entry)` happens *after* that external call returns: [4](#0-3) 

Meanwhile, `debt-add-scaled` explicitly documents and implements the oracle-frontrunning protection by stamping `last-borrow-block: stacks-block-height` on a freshly resolved entry and committing it via its own `insert`: [5](#0-4) 

If, during the `receive-tokens` callback inside `collateral-add`, the reentrant call path reaches `debt-add-scaled` for the same account (through the impl contract, satisfying `check-impl-auth`, which only checks `contract-caller == impl` and is unaware of the outer call already in progress), that nested call reads the registry (still pre-outer-insert state), sets the correct `last-borrow-block`, and commits it via `map-set`. Control then returns to `collateral-add`, which finishes evaluating its already-fixed `updated-entry` (captured before the nested write) and calls `insert`, clobbering the map row: `last-borrow-block` reverts to the pre-nested value and the debt mask bit set by the nested call is also erased (since `updated-mask` in the outer call was computed from the mask *before* the nested debt bit was added). No check in `collateral-add`, `insert`, or `refresh` detects or prevents this because none of them re-read the map state before writing - `insert` performs an unconditional `map-set`.

### Impact Explanation
`last-borrow-block` is the protocol's explicit anti-oracle-frontrunning safeguard, gating same-block borrow-then-act sequences. This clobber silently discards a just-committed, correct `last-borrow-block` and reinstates a stale one, nullifying that guard for the affected account within the same transaction. This is an unauthorized state change caused by contract logic (not user-supplied data), and it removes a security control that other code paths rely on to prevent same-block price-staleness exploitation — categorized as High per the impact taxonomy (temporary loss of a fund-protecting safeguard, enabling downstream frontrunning-based extraction of value in the same block).

### Likelihood Explanation
Reachability depends entirely on whether the calling implementation contract's flow allows a nested call back into `debt-add-scaled` (via the impl) to occur *during* the `ft`-transfer callback inside `collateral-add`, i.e., whether the impl contract (`v0-4-market.clar`, not read in this pass) lacks a reentrancy guard around its own borrow/deposit entrypoints. The attacker fully controls the `<ft-trait>` implementation passed to `collateral-add`, so triggering a callback on `transfer` is trivial and costs only gas/deployment of a malicious FT contract; whether that callback can reach `debt-add-scaled` for the same account before the outer call resumes is not confirmed from `v0-market-vault.clar` alone.

### Recommendation
Re-read the registry entry immediately before calling `insert` (or perform the `mask`/`last-borrow-block` update atomically against the latest map state, e.g., via a fetch-modify-write pattern executed after all external calls, or by moving `receive-tokens` before any read of `entry`, or by adding a reentrancy lock in the impl layer that prevents nested calls into the vault for the same account within one call).

### Proof of Concept
Clarinet/vitest simnet plan:
1. Deploy a malicious FT contract implementing `ft-trait` whose `transfer` function, on invocation, calls back into the market impl's borrow entrypoint (or directly the vault's `debt-add-scaled` if reachable) for the same account/asset, causing it to set `last-borrow-block` to the current block and commit.
2. Deploy/wire `v0-market-vault.clar` with `impl` set to a test-controlled principal that can call `collateral-add` and reproduce the callback chain.
3. Call `collateral-add` for `account` with the malicious FT as `ft`.
4. After the call returns, call `(lookup (resolve account))` and assert `last-borrow-block` equals the block height stamped by the nested `debt-add-scaled` call, not a stale/prior value; also assert the debt mask bit set by the nested call is present in `mask`.
5. Show that both assertions fail against current code (the registry entry reflects only the outer `collateral-add` snapshot), confirming the clobber.

### Citations

**File:** mainnet/contracts/market/v0-market-vault.clar (L171-171)
```text
(define-private (refresh (mask uint)) { mask: mask, last-update: stacks-block-time })
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

**File:** mainnet/contracts/market/v0-market-vault.clar (L442-456)
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
```
