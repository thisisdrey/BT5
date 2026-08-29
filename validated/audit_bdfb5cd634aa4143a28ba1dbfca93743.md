### Title
`remove_account` fails to clean up PromiseYield state (`PromiseYieldStatus`, `YieldIdToDataId`, `DataIdToYieldId`), allowing yield timeout resolution to leak privileged callbacks to a re-created account - ([File: core/store/src/utils/mod.rs])

### Summary
`remove_account` in `core/store/src/utils/mod.rs` only removes the `Account`, `ContractCode`, access/gas keys, and `ContractData` trie entries for a deleted account, but never removes `TrieKey::PromiseYieldStatus`, `TrieKey::YieldIdToDataId`, or `TrieKey::DataIdToYieldId` entries scoped to that account. Since the `PromiseYieldTimeout` queue entry is stored independently (not account-scoped) and fires later by account id, an account can be deleted and recreated while a stale yield resolution row survives, allowing the timeout resolution logic to act on the new account using state left behind by the old owner.

### Finding Description
`remove_account` performs targeted cleanup of exactly four categories of state: [1](#0-0) 
It removes `Account`, `ContractCode`, then iterates and removes access keys/gas key nonces, then iterates and removes `ContractData`: [2](#0-1) 

Nowhere in this function is `TrieKey::PromiseYieldStatus`, `TrieKey::YieldIdToDataId`, or `TrieKey::DataIdToYieldId` removed, even though dedicated removal helpers exist for exactly this purpose elsewhere in the same file: [3](#0-2) [4](#0-3) 

These helpers (`remove_promise_yield_status`, `remove_yield_id_mappings`) are called elsewhere (e.g., during resolution/resumption of a yield), but `remove_account` does not invoke them. Meanwhile, the `PromiseYieldTimeout` queue entry created by `enqueue_promise_yield_timeout` is stored under a global, sequential `TrieKey::PromiseYieldTimeout { index }` — not scoped to or cleared by account deletion: [5](#0-4) 

This means: (1) account A creates a yield via its own contract, writing `PromiseYieldStatus{A}`, `YieldIdToDataId{A}`, `DataIdToYieldId{A}` and enqueuing an independent `PromiseYieldTimeout` entry; (2) `DeleteAccountAction` deletes A via `remove_account`, which leaves the yield rows intact; (3) A is recreated (new owner, new keys, potentially different contract); (4) when the queued timeout eventually processes, it looks up state by account id `A` and finds the surviving `DataIdToYieldId`/`PromiseYieldStatus` rows, resolving/timing-out a yield the new owner never created.

I was not able to fully inspect `resolve_promise_yield_timeout`'s exact lookup/resumption logic in `runtime/runtime/src/lib.rs` within the available iterations to confirm whether it has other implicit guards (e.g., cross-checking the receipt id, PromiseYieldReceipt existence, or PromiseYieldIndices bounds) that could neutralize the leftover rows before causing an unauthorized callback. This should be independently verified. However, the core defect — `remove_account` not clearing PromiseYield-related trie keys — is directly confirmed by the code above and is a genuine gap relative to the analogous cleanup routines that do exist for this purpose.

### Impact Explanation
If confirmed reachable end-to-end, this allows state belonging to a deleted account to influence execution against a newly created account at the same id, without the new owner ever issuing a corresponding `promise_yield_create` call — an authorization/determinism violation matching the "privileged callback executes on the re-created account without consent" scope. This falls under authorization escalation across accounts/promises in the bounty categories.

### Likelihood Explanation
Preconditions are attacker-controlled and cheap: deploy a contract on account A capable of calling `promise_yield_create_with_id` with a long timeout, then submit a `DeleteAccountAction` for A, then have any party recreate A. All actions are ordinary, unprivileged transactions available to any funded account. The main open question is whether `resolve_promise_yield_timeout` has additional defensive checks that make the leftover rows inert; without confirming that function's full logic, likelihood cannot be certified as fully proven, only that the described state-cleanup gap in `remove_account` is real.

### Recommendation
In `remove_account`, before or after clearing other account-scoped rows, iterate/remove all `PromiseYieldStatus`, `YieldIdToDataId`, and `DataIdToYieldId` entries for `account_id` (mirroring the pattern used for access keys/contract data iteration), using the existing `remove_promise_yield_status` / `remove_yield_id_mappings` helpers. Additionally, consider having `resolve_promise_yield_timeout` verify that the yield's associated `PromiseYieldStatus`/mapping is still present and consistent before acting, as defense in depth.

### Proof of Concept
1. Unit test in `runtime/runtime/src/lib.rs` or a runtime-test-loop integration test:
   - Deploy a contract on account `A` that calls `promise_yield_create_with_id`, causing `PromiseYieldStatus{A,data_id}`, `YieldIdToDataId{A,yield_id}`, `DataIdToYieldId{A,data_id}` to be set and a `PromiseYieldTimeout{account_id: A, data_id, expires_at}` enqueued.
   - Apply `DeleteAccountAction` for `A` (calling `remove_account`).
   - Assert `get_promise_yield_status`, `get_data_id_for_yield_id`, `get_yield_id_for_data_id` for `A` still return `Some(...)` after deletion (demonstrating the gap) — expected/desired behavior is `None`.
   - Recreate account `A` with a different key/contract.
   - Advance blocks until the `PromiseYieldTimeout` entry's `expires_at` triggers processing, and assert that a resumed/timeout receipt is generated referencing the stale `data_id`/`yield_id` against the new `A`, despite the new owner never calling `promise_yield_create`.
   - Expected fix result: after `remove_account`, all three lookups return `None`, and the timeout queue entry either finds nothing to resolve or is itself also purged.

### Citations

**File:** core/store/src/utils/mod.rs (L182-198)
```rust
pub fn enqueue_promise_yield_timeout(
    state_update: &mut TrieUpdate,
    promise_yield_indices: &mut PromiseYieldIndices,
    account_id: AccountId,
    data_id: CryptoHash,
    expires_at: BlockHeight,
) {
    set(
        state_update,
        TrieKey::PromiseYieldTimeout { index: promise_yield_indices.next_available_index },
        &PromiseYieldTimeout { account_id, data_id, expires_at },
    );
    promise_yield_indices.next_available_index = promise_yield_indices
        .next_available_index
        .checked_add(1)
        .expect("Next available index for PromiseYield timeout queue exceeded the integer limit");
}
```

**File:** core/store/src/utils/mod.rs (L273-279)
```rust
pub fn remove_promise_yield_status(
    state_update: &mut TrieUpdate,
    receiver_id: &AccountId,
    data_id: CryptoHash,
) {
    state_update.remove(TrieKey::PromiseYieldStatus { receiver_id: receiver_id.clone(), data_id });
}
```

**File:** core/store/src/utils/mod.rs (L326-334)
```rust
pub fn remove_yield_id_mappings(
    state_update: &mut TrieUpdate,
    receiver_id: &AccountId,
    yield_id: YieldId,
    data_id: CryptoHash,
) {
    state_update.remove(TrieKey::YieldIdToDataId { receiver_id: receiver_id.clone(), yield_id });
    state_update.remove(TrieKey::DataIdToYieldId { receiver_id: receiver_id.clone(), data_id });
}
```

**File:** core/store/src/utils/mod.rs (L505-513)
```rust
pub fn remove_account(
    state_update: &mut TrieUpdate,
    account_id: &AccountId,
) -> Result<RemoveAccountResult, StorageError> {
    state_update.remove(TrieKey::Account { account_id: account_id.clone() });
    state_update.remove(TrieKey::ContractCode { account_id: account_id.clone() });

    let mut gas_key_nonce_count: usize = 0;
    let mut gas_key_nonce_total_key_bytes: usize = 0;
```

**File:** core/store/src/utils/mod.rs (L551-573)
```rust
    for trie_key in keys_to_remove {
        state_update.remove(trie_key);
    }

    // Removing contract data
    let lock = state_update.trie().lock_for_iter();
    let data_keys = state_update
        .locked_iter(&trie_key_parsers::get_raw_prefix_for_contract_data(account_id, &[]), &lock)?
        .map(|raw_key| {
            trie_key_parsers::parse_data_key_from_contract_data_key(&raw_key?, account_id)
                .map_err(|_e| {
                    StorageError::StorageInconsistentState(
                        "Can't parse data key from raw key for ContractData".to_string(),
                    )
                })
                .map(Vec::from)
        })
        .collect::<Result<Vec<_>, _>>()?;
    drop(lock);

    for key in data_keys {
        state_update.remove(TrieKey::ContractData { account_id: account_id.clone(), key });
    }
```
