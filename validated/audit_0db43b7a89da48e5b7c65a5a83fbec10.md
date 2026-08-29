### Title
`remove_account` fails to clear `YieldIdToDataId`/`DataIdToYieldId`/`PromiseYieldReceipt`/`PromiseYieldStatus` rows, causing cross-incarnation yield-id and receipt leakage - (File: `core/store/src/utils/mod.rs`)

### Summary
`remove_account` only deletes `TrieKey::Account`, `TrieKey::ContractCode`, access keys, gas-key nonces, and `TrieKey::ContractData` rows, but never touches `TrieKey::YieldIdToDataId`, `TrieKey::DataIdToYieldId`, `TrieKey::PromiseYieldStatus`, or `TrieKey::PromiseYieldReceipt` rows written by `set_yield_id_mapping`/`create_promise_yield_receipt_with_id`. Because these keys are namespaced only by `account_id` (which is reusable after deletion), a fresh incarnation of the same account name inherits stale yield/receipt state from the deleted incarnation.

### Finding Description
`remove_account` (`core/store/src/utils/mod.rs:505-575`) explicitly enumerates and removes:
- `TrieKey::Account` [1](#0-0) 
- access keys and gas-key nonces via prefix iteration [2](#0-1) 
- `TrieKey::ContractData` via prefix iteration [3](#0-2) 

It contains no logic that iterates or removes `TrieKey::YieldIdToDataId`, `TrieKey::DataIdToYieldId`, `TrieKey::PromiseYieldStatus`, or `TrieKey::PromiseYieldReceipt` for the account being deleted, even though a dedicated cleanup helper `remove_yield_id_mappings` already exists elsewhere in the same file and is simply never invoked from `remove_account` [4](#0-3) .

Meanwhile, `set_yield_id_mapping` (called from `create_promise_yield_receipt_with_id`) writes both directions of the mapping keyed only on `receiver_id` + `yield_id`/`data_id`: [5](#0-4) 

`create_promise_yield_receipt_with_id` in `runtime/runtime/src/ext.rs` uses `has_yield_id_mapping` purely as a trie-state check to decide whether a `user_yield_id` is a duplicate, with no notion of "generation" or account-incarnation isolation: [6](#0-5) 

Exploit/repro flow:
1. `victim.near` calls `promise_yield_create_with_id(Y)` — this writes `YieldIdToDataId{victim.near, Y} -> D` and `DataIdToYieldId{victim.near, D} -> Y`, plus a `PromiseYieldStatus{victim.near, D}` and a `PromiseYieldReceipt{victim.near, D}`.
2. `victim.near` is deleted via `DeleteAccount` action, which calls `remove_account`. None of the yield-related rows are removed.
3. Anyone (an unprivileged attacker) recreates `victim.near` via `CreateAccount`.
4. The new owner's contract on `victim.near` calls `promise_yield_create_with_id(Y)` again. `has_yield_id_mapping` still finds the stale `YieldIdToDataId{victim.near, Y}` row and incorrectly reports it as already used, so `create_promise_yield_receipt_with_id` returns `None` — a spurious duplicate rejection unrelated to anything the new incarnation did.
5. More severely, because `PromiseYieldReceipt{victim.near, D}` and `PromiseYieldStatus{victim.near, D}` also survive, and the corresponding `PromiseYieldTimeout` queue entry (never cleared by `remove_account` either) still references `(victim.near, D)`, a resume/timeout for the *old* incarnation's yield can still resolve and execute the stale receipt in the context of the *new* incarnation once that timeout height is reached or if `data_id` `D` is submitted via `submit_promise_resume_data`, which checks `has_promise_yield_receipt`/`has_promise_yield_status` on the current (new) account state without any incarnation check: [7](#0-6) 

### Impact Explanation
This is a genuine account-lifecycle/state-isolation break: state written by one incarnation of an account is not purged on deletion and leaks into the next incarnation of the same account name. Concretely this causes:
- Functional corruption: legitimate `promise_yield_create_with_id` calls on a freshly created account can be wrongly rejected as duplicates due to stale data from a previous, unrelated owner of the same account name.
- Potential authorization/execution-integrity issue: since the stale `PromiseYieldReceipt`/timeout entry for the deleted incarnation is not purged, a resume/timeout tied to the old owner's receipt can still fire and be processed against the new incarnation's account, executing a receipt whose contents (method, args, gas) were determined by the previous account owner — this crosses the "state isolation between account incarnations" invariant and could result in unexpected/attacker-crafted receipt execution against the new contract's state (a promise/authorization-boundary violation rather than a signature/access-key bypass).

This does not directly cause fund theft through a signature or access-key bypass, but it is a state-determinism/isolation bug with a scoped impact matching "authorization escalation across accounts or promises" in the more severe sub-case, and at minimum a reliable state-corruption/self-DoS bug in the duplicate-detection sub-case.

### Likelihood Explanation
- No special privilege is needed: any account holder can call `promise_yield_create_with_id`, delete their own account, and any other unprivileged user can recreate an account with that name once it is deleted (subject to normal account-creation rules).
- The bug is deterministic and 100% reproducible — it does not depend on timing, races, or validator behavior.
- The main precondition is that the account owner deletes the account while a yield/receipt entry is still pending (unresolved), which is under the account owner's control, and that someone (attacker or otherwise) later recreates the same account name.

### Recommendation
Extend `remove_account` in `core/store/src/utils/mod.rs` to also enumerate and remove all `TrieKey::YieldIdToDataId`, `TrieKey::DataIdToYieldId`, `TrieKey::PromiseYieldStatus`, and `TrieKey::PromiseYieldReceipt` rows scoped to the account being deleted (mirroring the existing prefix-iteration pattern used for access keys and contract data, and reusing `remove_yield_id_mappings`/`remove_promise_yield_receipt`/`remove_promise_yield_status`). Additionally, any pending `PromiseYieldTimeout` queue entries referencing the deleted account should be treated as no-ops (skip execution) if the receiver account was deleted/recreated since the timeout was enqueued, e.g., by checking incarnation identity or clearing status before dispatch.

### Proof of Concept
Unit test in `core/store/src/utils/mod.rs` test module (or a new test file in the same crate):
1. Build a `TrieUpdate` (via existing test utilities such as `test_utils`), pick `account_id = "victim.near"`, `yield_id = Y`, `data_id = D`.
2. Call `set_yield_id_mapping(&mut state_update, &account_id, Y, D)`.
3. Assert `get_data_id_for_yield_id(&state_update, &account_id, Y) == Some(D)` and `get_yield_id_for_data_id(&state_update, &account_id, D) == Some(Y)`.
4. Call `remove_account(&mut state_update, &account_id)`.
5. Assert `get_data_id_for_yield_id(&state_update, &account_id, Y)` and `get_yield_id_for_data_id(&state_update, &account_id, D)` still return `Some`, proving the rows survive account removal.
6. (Extended integration test in `test-loop-tests`) Simulate: create account, call `promise_yield_create_with_id(Y)`, delete account, recreate account with same name, call `promise_yield_create_with_id(Y)` again from the new contract, and assert the host call incorrectly returns "duplicate" (`None`) even though the new incarnation never created that yield id.

### Citations

**File:** core/store/src/utils/mod.rs (L281-297)
```rust
pub fn set_yield_id_mapping(
    state_update: &mut TrieUpdate,
    receiver_id: &AccountId,
    yield_id: YieldId,
    data_id: CryptoHash,
) {
    set(
        state_update,
        TrieKey::YieldIdToDataId { receiver_id: receiver_id.clone(), yield_id },
        &data_id,
    );
    set(
        state_update,
        TrieKey::DataIdToYieldId { receiver_id: receiver_id.clone(), data_id },
        &yield_id,
    );
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

**File:** core/store/src/utils/mod.rs (L509-510)
```rust
    state_update.remove(TrieKey::Account { account_id: account_id.clone() });
    state_update.remove(TrieKey::ContractCode { account_id: account_id.clone() });
```

**File:** core/store/src/utils/mod.rs (L516-553)
```rust
    let lock = state_update.trie().lock_for_iter();
    let mut keys_to_remove: Vec<TrieKey> = Vec::new();
    for raw_key in state_update
        .locked_iter(&trie_key_parsers::get_raw_prefix_for_access_keys(account_id), &lock)?
    {
        let raw_key = raw_key?;
        let key_handle = trie_key_parsers::parse_key_handle_from_access_key_key(
            &raw_key, account_id,
        )
        .map_err(|_e| {
            StorageError::StorageInconsistentState(
                "Can't parse key handle from raw key for AccessKey".to_string(),
            )
        })?;
        let nonce_index =
            trie_key_parsers::parse_nonce_index_from_gas_key_key(&raw_key, account_id, &key_handle)
                .map_err(|_e| {
                    StorageError::StorageInconsistentState(
                        "Can't parse nonce index from raw key for AccessKey".to_string(),
                    )
                })?;
        if let Some(index) = nonce_index {
            gas_key_nonce_count += 1;
            gas_key_nonce_total_key_bytes += raw_key.len();
            keys_to_remove.push(TrieKey::gas_key_nonce(
                account_id.clone(),
                key_handle.clone(),
                index,
            ));
        } else {
            keys_to_remove.push(TrieKey::access_key(account_id.clone(), key_handle.clone()));
        }
    }
    drop(lock);

    for trie_key in keys_to_remove {
        state_update.remove(trie_key);
    }
```

**File:** core/store/src/utils/mod.rs (L555-573)
```rust
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

**File:** runtime/runtime/src/ext.rs (L371-400)
```rust
    fn create_promise_yield_receipt_with_id(
        &mut self,
        receiver_id: AccountId,
        user_yield_id: YieldId,
    ) -> Result<Option<(ReceiptIndex, CryptoHash)>, VMLogicError> {
        // Check for duplicate yield_id in trie. TrieUpdate also reflects writes from earlier
        // calls within the same function call, so this also catches in-transaction duplicates.
        if has_yield_id_mapping(self.trie_update, &receiver_id, user_yield_id)
            .map_err(wrap_storage_error)?
        {
            return Ok(None);
        }

        let input_data_id = self.generate_data_id();

        // Store bidirectional yield_id <-> data_id mappings
        set_yield_id_mapping(&mut self.trie_update, &receiver_id, user_yield_id, input_data_id);

        let receipt_index =
            self.receipt_manager.create_promise_yield_receipt(input_data_id, receiver_id.clone());

        set_promise_yield_status(
            &mut self.trie_update,
            &receiver_id,
            input_data_id,
            PromiseYieldStatus::Yielded,
        );

        Ok(Some((receipt_index, input_data_id)))
    }
```

**File:** runtime/runtime/src/ext.rs (L402-420)
```rust
    fn submit_promise_resume_data(
        &mut self,
        data_id: CryptoHash,
        data: Vec<u8>,
    ) -> Result<bool, VMLogicError> {
        let has_yield_receipt_in_state =
            has_promise_yield_receipt(self.trie_update, self.account_id.clone(), data_id)
                .map_err(wrap_storage_error)?;
        let has_yield_status_in_state =
            has_promise_yield_status(self.trie_update, &self.account_id, data_id)
                .map_err(wrap_storage_error)?;

        if has_yield_receipt_in_state || has_yield_status_in_state {
            self.receipt_manager.create_promise_resume_receipt(data_id, data);
            set_promise_yield_status(
                &mut self.trie_update,
                &self.account_id,
                data_id,
                PromiseYieldStatus::ResumeInitiated,
```
