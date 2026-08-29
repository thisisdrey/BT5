### Title
`remove_account` fails to clear `TrieKey::PromiseYieldStatus`/`PromiseYieldReceipt`/yield-id mappings, allowing stale yield state to bind to a recreated account - ([File: core/store/src/utils/mod.rs])

### Summary
`remove_account` in `core/store/src/utils/mod.rs` only removes the `Account`, `ContractCode`, access-key/gas-key, and `ContractData` trie entries for a deleted account, but never touches `TrieKey::PromiseYieldStatus`, `TrieKey::PromiseYieldReceipt`, `TrieKey::YieldIdToDataId`, or `TrieKey::DataIdToYieldId` entries keyed by the same `account_id`/`receiver_id`. An attacker who calls `promise_yield_create` against their own account, then issues `DeleteAccount`, and recreates an account with the same id, leaves these promise-yield rows orphaned in the trie under the reused account name.

### Finding Description
`remove_account` is the single cleanup routine invoked when an account is removed via the `DeleteAccount` action, and it explicitly enumerates what it deletes: [1](#0-0) 
It then walks and removes access keys/gas-key nonces and contract data: [2](#0-1) 

Nowhere in this function is `TrieKey::PromiseYieldStatus`, `TrieKey::PromiseYieldReceipt`, `TrieKey::YieldIdToDataId`, or `TrieKey::DataIdToYieldId` removed, even though dedicated helper functions for clearing these exist elsewhere in the same module (`remove_promise_yield_status`, `remove_promise_yield_receipt`, `remove_yield_id_mappings`): [3](#0-2) [4](#0-3) 

These helpers are normally invoked only along the resume/timeout completion paths in `runtime/runtime/src/lib.rs`, not from `remove_account`. Since `TrieKey::PromiseYieldStatus { receiver_id, data_id }` and `TrieKey::PromiseYieldReceipt { receiver_id, data_id }` are keyed purely by account name and a data id the attacker itself chose when calling `promise_yield_create`, an attacker can:
1. Call `promise_yield_create` on their own contract, capturing the resulting `data_id`.
2. Submit `DeleteAccount` for that account (removing `Account`/`ContractCode`/keys/data, but leaving the `PromiseYieldStatus`/`PromiseYieldReceipt`/yield-id-mapping rows intact under the same account name in the trie).
3. Recreate an account with the identical id (e.g., via `CreateAccount`), deploying new code/keys.
4. Trigger the yield-resume path (submitting a resume-shaped receipt referencing the original, attacker-known `data_id`) against the now-different account.

Because the stale `PromiseYieldStatus`/`PromiseYieldReceipt` rows are still present and keyed to the reused `receiver_id`, the resume-processing logic (which looks these rows up purely by `receiver_id`/`data_id`, per `get_promise_yield_status`/`get_promise_yield_receipt`) has no way to distinguish "this row belongs to the old, deleted account incarnation" from "this row belongs to the current account." This breaks the invariant that a resumed promise outcome should not exist for an account that never created it in its current lifetime.

I was not able to fully trace, within this session, the exact runtime code in `runtime/runtime/src/lib.rs` that consumes `PromiseYieldStatus`/`PromiseYieldReceipt` during data-receipt/resume processing (49 references were found but not read line-by-line), so I cannot independently confirm whether some other check (e.g., matching against `PromiseYieldIndices` or a version/generation nonce) additionally guards against this specific staleness. This is a gap in my verification, not a claim that the exploit is blocked — the `remove_account` code itself, which is the object of the audit question, unambiguously does not clear these keys.

### Impact Explanation
If unguarded downstream, this allows a postponed receipt created by the deleted account's promise-yield call to be resumed and executed in the context of a semantically different (recreated) account, which corresponds to state-root divergence and authorization/promise-binding confusion — a receipt/action executing against an account that never authorized it in its current incarnation.

### Likelihood Explanation
The attacker only needs to control their own account: fund it, deploy a contract that calls `promise_yield_create`, delete the account, and recreate it — all standard, unprivileged transaction types (`FunctionCall`, `DeleteAccount`, `CreateAccount`). The `data_id` is known to the attacker because they generated it themselves. The main open question is whether the resume-consumption code path has an independent staleness guard that would neutralize the leftover rows; that could not be confirmed within this session's tool budget.

### Recommendation
Extend `remove_account` in `core/store/src/utils/mod.rs` to also enumerate and remove any `TrieKey::PromiseYieldStatus`, `TrieKey::PromiseYieldReceipt`, `TrieKey::YieldIdToDataId`, and `TrieKey::DataIdToYieldId` entries associated with the account being deleted (e.g., via prefix iteration similar to the access-key/contract-data removal loops already present in the function), and/or add a defensive check in the resume/timeout processing code that verifies the current account "generation"/existence before honoring a resume for a given `(receiver_id, data_id)`.

### Proof of Concept
Unit/integration test plan (table-test as suggested by the audit question):
1. Enumerate every `TrieKey` variant carrying `receiver_id`/`account_id` (`Account`, `ContractCode`, `AccessKey`, `GasKeyNonce`, `ContractData`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, `DataIdToYieldId`, `PostponedReceipt`, `ReceivedData`, etc.).
2. For each, mark whether it is written by `promise_yield_create` (or its associated code paths) and whether it is cleared by `remove_account`.
3. Assert that `PromiseYieldStatus`, `PromiseYieldReceipt`, `YieldIdToDataId`, `DataIdToYieldId` are in the "written" set but absent from the "cleared by `remove_account`" set — confirmed true by direct code inspection above.
4. Runtime/test-loop integration test: create account `alice`, have it call `promise_yield_create` capturing `data_id`; submit `DeleteAccount` for `alice`; recreate `alice` with new keys/contract; submit a yield-resume receipt referencing the original `data_id`; assert whether the postponed receipt executes against the new `alice` account (expected: it should not, since `alice`-v2 never created that yield) — this final runtime-behavior assertion requires tracing the resume-consumption code in `runtime/runtime/src/lib.rs`, which was not completed in this session.

### Citations

**File:** core/store/src/utils/mod.rs (L214-279)
```rust
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

pub fn has_promise_yield_receipt(
    trie: &dyn TrieAccess,
    receiver_id: AccountId,
    data_id: CryptoHash,
) -> Result<bool, StorageError> {
    trie.contains_key(
        &TrieKey::PromiseYieldReceipt { receiver_id, data_id },
        AccessOptions::DEFAULT,
    )
}

pub fn get_promise_yield_status(
    trie: &dyn TrieAccess,
    receiver_id: &AccountId,
    data_id: CryptoHash,
) -> Result<Option<PromiseYieldStatus>, StorageError> {
    get(trie, &TrieKey::PromiseYieldStatus { receiver_id: receiver_id.clone(), data_id })
}

pub fn has_promise_yield_status(
    trie: &dyn TrieAccess,
    receiver_id: &AccountId,
    data_id: CryptoHash,
) -> Result<bool, StorageError> {
    trie.contains_key(
        &TrieKey::PromiseYieldStatus { receiver_id: receiver_id.clone(), data_id },
        AccessOptions::DEFAULT,
    )
}

pub fn set_promise_yield_status(
    state_update: &mut TrieUpdate,
    receiver_id: &AccountId,
    data_id: CryptoHash,
    status: PromiseYieldStatus,
) {
    set(
        state_update,
        TrieKey::PromiseYieldStatus { receiver_id: receiver_id.clone(), data_id },
        &status,
    );
}

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

**File:** core/store/src/utils/mod.rs (L504-510)
```rust
/// Removes account, code and all access keys and gas keys associated to it.
pub fn remove_account(
    state_update: &mut TrieUpdate,
    account_id: &AccountId,
) -> Result<RemoveAccountResult, StorageError> {
    state_update.remove(TrieKey::Account { account_id: account_id.clone() });
    state_update.remove(TrieKey::ContractCode { account_id: account_id.clone() });
```

**File:** core/store/src/utils/mod.rs (L515-573)
```rust
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
```
