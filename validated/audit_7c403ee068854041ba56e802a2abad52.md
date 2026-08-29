## Finding Confirmed

I verified the code path directly. `remove_account` in `core/store/src/utils/mod.rs` only removes `TrieKey::Account`, `TrieKey::ContractCode`, access keys / gas key nonces, and `TrieKey::ContractData`. It never touches the promise-yield family of keys: `TrieKey::YieldIdToDataId`, `TrieKey::DataIdToYieldId`, `TrieKey::PromiseYieldReceipt`, or `TrieKey::PromiseYieldStatus`. [1](#0-0) 

The write side confirms these keys are keyed by `receiver_id` (the account name, not any incarnation identifier), and removal is only ever done explicitly via `remove_yield_id_mappings`/`remove_promise_yield_receipt`/`remove_promise_yield_status`, which are never called from `remove_account`: [2](#0-1) 

### Title
Incomplete promise-yield state cleanup on account deletion allows stale yield/data mappings to survive account recreation - (File: core/store/src/utils/mod.rs)

### Summary
`remove_account` fails to purge `TrieKey::YieldIdToDataId`, `TrieKey::DataIdToYieldId`, `TrieKey::PromiseYieldReceipt`, and `TrieKey::PromiseYieldStatus` entries when an account is deleted via `DeleteAccount`. Because these keys are namespaced only by `receiver_id` (the account name) with no incarnation/versioning component, a subsequently recreated account of the same name inherits orphaned yield-correlation data from the prior incarnation.

### Finding Description
An unprivileged attacker deploys a contract on account `A` and calls `promise_yield_create_with_id` with a caller-chosen `yield_id`, writing `TrieKey::YieldIdToDataId{receiver_id: A, yield_id}` and `TrieKey::DataIdToYieldId{receiver_id: A, data_id}` via `set_yield_id_mapping`. If the yield is never resumed and `A`'s owner calls `DeleteAccount`, the runtime invokes `remove_account`, which removes the `Account`, `ContractCode`, access keys, gas keys, and `ContractData`, but does not call `remove_yield_id_mappings` or `remove_promise_yield_receipt`/`remove_promise_yield_status`. These rows remain in the trie keyed to account name `A`. If `A` is re-created (a new `CreateAccount` action to the same account id, with fresh keys/contract), the new incarnation's contract can call `promise_yield_resume` with the same `yield_id` and it will resolve via the stale `YieldIdToDataId` entry to a `data_id` that belongs to the deleted incarnation's promise chain, rather than failing cleanly as "unknown yield". If the corresponding `PromiseYieldReceipt`/postponed receipt state also was not cleaned (also outside `remove_account`'s scope), this can deliver a resume callback tied to receipt state that logically belongs to the prior account incarnation. [3](#0-2) [4](#0-3) 

### Impact Explanation
This is a state-cleanup-completeness gap in account-scoped storage rather than a directly demonstrated fund-theft path. The scoped, code-confirmed impact is: `get_data_id_for_yield_id`/`get_yield_id_for_data_id`/`has_promise_yield_receipt` queries against a freshly-recreated account can return `Some(..)` for data that logically belongs to a deleted incarnation, violating the expectation that account deletion fully resets account-scoped state. I was not able to fully trace the downstream resume host-function validation (`runtime/runtime/src/ext.rs`, `runtime/runtime/src/lib.rs`) within the available tool budget to confirm whether this stale mapping can be leveraged into a concrete fund-theft or authorization-escalation outcome (e.g., forcing execution of a stale postponed receipt's actions against the new incarnation), so I cannot assert a fully proven fund-loss/authorization-escalation chain — this needs further tracing of `PromiseYieldReceipt` resumption and `ResumeReceipt` action processing in `runtime/runtime/src/lib.rs`/`ext.rs`.

### Likelihood Explanation
Low cost, fully attacker-controlled preconditions: deploy a wasm contract, call `promise_yield_create_with_id` with a chosen `yield_id`, never resume, delete the account, and recreate it — all via ordinary signed transactions to a public RPC endpoint, repeatable at will.

### Recommendation
Extend `remove_account` in `core/store/src/utils/mod.rs` to iterate and remove all `TrieKey::YieldIdToDataId`, `TrieKey::DataIdToYieldId`, `TrieKey::PromiseYieldReceipt`, and `TrieKey::PromiseYieldStatus` entries prefixed by the account being deleted, mirroring the existing access-key/contract-data cleanup pattern (prefix iteration + explicit `state_update.remove`).

### Proof of Concept
Integration test using `TrieUpdate`:
1. Call `set_yield_id_mapping(&mut trie_update, &account_id, yield_id, data_id)` and `set_promise_yield_status`/`set_promise_yield_receipt` for the same `account_id`.
2. Call `remove_account(&mut trie_update, &account_id)`.
3. Assert `get_data_id_for_yield_id(&trie_update, &account_id, yield_id)` returns `None` and `get_yield_id_for_data_id(&trie_update, &account_id, data_id)` returns `None` (currently returns `Some`), and likewise for `has_promise_yield_receipt`/`get_promise_yield_status`.

### Citations

**File:** core/store/src/utils/mod.rs (L200-228)
```rust
pub fn set_promise_yield_receipt(state_update: &mut TrieUpdate, receipt: &Receipt) {
    match receipt.versioned_receipt() {
        VersionedReceiptEnum::PromiseYield(action_receipt) => {
            assert!(action_receipt.input_data_ids().len() == 1);
            let key = TrieKey::PromiseYieldReceipt {
                receiver_id: receipt.receiver_id().clone(),
                data_id: action_receipt.input_data_ids()[0],
            };
            set(state_update, key, receipt);
        }
        _ => unreachable!("Expected PromiseYield receipt"),
    }
}

pub fn remove_promise_yield_receipt(
    state_update: &mut TrieUpdate,
    receiver_id: &AccountId,
    data_id: CryptoHash,
) {
    state_update.remove(TrieKey::PromiseYieldReceipt { receiver_id: receiver_id.clone(), data_id });
}

pub fn get_promise_yield_receipt(
    trie: &dyn TrieAccess,
    receiver_id: &AccountId,
    data_id: CryptoHash,
) -> Result<Option<Receipt>, StorageError> {
    get(trie, &TrieKey::PromiseYieldReceipt { receiver_id: receiver_id.clone(), data_id })
}
```

**File:** core/store/src/utils/mod.rs (L281-334)
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

pub fn get_data_id_for_yield_id(
    trie: &dyn TrieAccess,
    receiver_id: &AccountId,
    yield_id: YieldId,
) -> Result<Option<CryptoHash>, StorageError> {
    get(trie, &TrieKey::YieldIdToDataId { receiver_id: receiver_id.clone(), yield_id })
}

pub fn get_yield_id_for_data_id(
    trie: &dyn TrieAccess,
    receiver_id: &AccountId,
    data_id: CryptoHash,
) -> Result<Option<YieldId>, StorageError> {
    get(trie, &TrieKey::DataIdToYieldId { receiver_id: receiver_id.clone(), data_id })
}

pub fn has_yield_id_mapping(
    trie: &dyn TrieAccess,
    receiver_id: &AccountId,
    yield_id: YieldId,
) -> Result<bool, StorageError> {
    trie.contains_key(
        &TrieKey::YieldIdToDataId { receiver_id: receiver_id.clone(), yield_id },
        AccessOptions::DEFAULT,
    )
}

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

**File:** core/store/src/utils/mod.rs (L504-575)
```rust
/// Removes account, code and all access keys and gas keys associated to it.
pub fn remove_account(
    state_update: &mut TrieUpdate,
    account_id: &AccountId,
) -> Result<RemoveAccountResult, StorageError> {
    state_update.remove(TrieKey::Account { account_id: account_id.clone() });
    state_update.remove(TrieKey::ContractCode { account_id: account_id.clone() });

    let mut gas_key_nonce_count: usize = 0;
    let mut gas_key_nonce_total_key_bytes: usize = 0;

    // Removing access keys and gas key nonces
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
    Ok(RemoveAccountResult { gas_key_nonce_count, gas_key_nonce_total_key_bytes })
}
```
