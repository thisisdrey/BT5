### Title
`remove_account` fails to purge `ReceivedData`/`PostponedReceiptId`/`PendingDataCount`/`PostponedReceipt` (and Promise-Yield keys), letting postponed receipts execute against a re-created account - ([File: core/store/src/utils/mod.rs])

### Summary
`remove_account` (invoked from `action_delete_account`) only removes the `Account`, `ContractCode`, `AccessKey`/`GasKeyNonce`, and `ContractData` trie entries for the deleted account, while `TrieKey::get_account_id` shows several other per-account key variants that are keyed by `receiver_id`/`account_id` — `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, `DataIdToYieldId` — are never cleared. If an account with a pending multi-dependency receipt (e.g. `promise_and`) is deleted and later re-created, the stale postponed receipt remains in the trie under the account's namespace and will later execute against the new account.

### Finding Description
`remove_account` in [1](#0-0)  removes exactly four categories of trie state: `TrieKey::Account`, `TrieKey::ContractCode`, iterated `AccessKey`/`GasKeyNonce` entries under the access-key prefix, and iterated `ContractData` entries. It performs no iteration or removal over the receipt/data-dependency namespaces.

`TrieKey::get_account_id`, however, demonstrates that many more key variants are logically owned by an account, including `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, and `PostponedReceipt`: [2](#0-1) , as well as `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, `DataIdToYieldId`: [3](#0-2) .

`action_delete_account` calls `remove_account` directly with no additional cleanup of these namespaces: [4](#0-3) . The only guard on deletion is a storage-usage cap (`DeleteAccountWithLargeState`) computed from `account.storage_usage()` [5](#0-4) ; this accounting is driven by explicit `storage_usage` bookkeeping on access keys/contract code/data (e.g. `access_key_storage_usage`) and does not track postponed-receipt or received-data trie entries, so a pending cross-shard/local promise dependency does not inflate `storage_usage` and cannot block deletion.

**Attack flow:**
1. Attacker (as an ordinary account, no privileges needed) sends a receipt/transaction targeting account A with a `promise_and`-style two-input callback, so that one dependency is satisfied first. This causes the runtime to persist `PendingDataCount`, `PostponedReceiptId`, and later `PostponedReceipt`/`ReceivedData` entries keyed by `receiver_id = A` in A's trie namespace.
2. Before the second dependent input arrives, the owner of A (or anyone able to trigger `DeleteAccount` on A, e.g. A's own key) deletes A via `Action::DeleteAccount`. `action_delete_account` → `remove_account` clears the account/keys/contract/data but leaves the postponed-receipt bookkeeping untouched.
3. A is re-created (a fresh `CreateAccount` + key/contract deploy) by the attacker or a third party, since account non-existence is the only precondition for `CreateAccount`.
4. The second dependency arrives; the runtime finds the stale `PendingDataCount`/`PostponedReceiptId`/`ReceivedData` entries under A and dispatches the surviving `PostponedReceipt`, which executes its `ActionReceipt` (e.g. a `FunctionCall`) against the **new** account A — including the new account's freshly deployed contract code and state — even though the original sender authorized/addressed the call to the old A.

No existing signature, nonce, access-key, or storage-staking check intercepts this because the check set is scoped to the deleting account's live storage usage, not to leftover postponed-receipt/received-data namespaces, and account re-creation has no notion of "was this account previously deleted with pending cross-call state."

### Impact Explanation
This breaks state determinism and cross-contract call authorization guarantees: a receipt originally destined for one account's contract/state ends up executing against a different, attacker-controlled account and contract after re-creation, effectively giving the new account owner control over (or corrupting) execution flow, deposits, and access-key-authorization context that a receiving contract implicitly trusts to belong to the same logical account instance. This falls under "authorization escalation across accounts or promises" in the bounty categories, with potential for stolen/misdirected funds or corrupted contract state depending on what the stale postponed action does.

### Likelihood Explanation
The precondition (delete-then-recreate an account with an in-flight multi-dependency promise) is fully reachable by an ordinary account holder with a full-access key on their own account and no elevated privileges — it only requires issuing a `promise_and`/callback pattern receipt, then a `DeleteAccount` action, then a `CreateAccount` action, all via normal signed transactions/contract calls. It is repeatable and low-cost (just gas + minimal deposits), though it requires the account owner's cooperation in deleting/recreating their own account, or targeting an account they control, which narrows — but does not eliminate — real-world impact scenarios (e.g. asset custody in contracts that use deferred promise callbacks tied to account identity).

### Recommendation
Extend `remove_account` (or add a companion cleanup routine called from `action_delete_account`) to iterate and remove all account-scoped receipt/data-dependency trie prefixes before finalizing deletion: `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, and the Promise-Yield family (`PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, `DataIdToYieldId`). Alternatively, block `DeleteAccount` while any pending postponed-receipt/data-dependency state exists for the account (mirroring the existing `DeleteAccountWithLargeState` style guard), forcing cleanup or rejection rather than silent orphaning.

### Proof of Concept
Rust integration test in `runtime/runtime/src/tests/` (or `runtime/runtime/src/actions.rs` test module):
1. Set up account A with a full-access key and a trivial contract.
2. Simulate a two-input `promise_and` callback targeting A: manually `set` a `PendingDataCount`, `PostponedReceiptId`, and `PostponedReceipt` (via `near_store` setters) plus one `ReceivedData` entry for A, mimicking state after one dependency resolved.
3. Call `action_delete_account` for A and commit.
4. Assert (table test) that `written_keys` (the full set of trie keys with `get_account_id() == Some(A)` written in step 2) minus `cleared_keys` (keys actually removed by `remove_account`) is non-empty — specifically that `PendingDataCount`, `PostponedReceiptId`, `PostponedReceipt`, `ReceivedData` for A still exist in the trie after deletion via `get`/iteration lookups.
5. Re-create account A, then supply the second dependency and run `apply`; assert the stale postponed receipt executes and mutates the new A's state, proving cross-account leakage.

### Citations

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

**File:** core/primitives/src/trie_key.rs (L596-602)
```rust
            TrieKey::ReceivedData { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::PostponedReceiptId { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::PendingDataCount { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::PostponedReceipt { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::DelayedReceiptIndices => None,
            TrieKey::DelayedReceipt { .. } => None,
            TrieKey::ContractData { account_id, .. } => Some(account_id.clone()),
```

**File:** core/primitives/src/trie_key.rs (L605-617)
```rust
            TrieKey::PromiseYieldReceipt { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::BufferedReceiptIndices => None,
            TrieKey::BufferedReceipt { .. } => None,
            TrieKey::BandwidthSchedulerState => None,
            TrieKey::BufferedReceiptGroupsQueueData { .. } => None,
            TrieKey::BufferedReceiptGroupsQueueItem { .. } => None,
            // Even though global contract code might be deployed under account id, it doesn't
            // correspond to the data stored for that account id, so always returning None here.
            TrieKey::GlobalContractCode { .. } => None,
            TrieKey::GlobalContractNonce { .. } => None,
            TrieKey::PromiseYieldStatus { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::YieldIdToDataId { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::DataIdToYieldId { receiver_id, .. } => Some(receiver_id.clone()),
```

**File:** runtime/runtime/src/actions.rs (L326-353)
```rust
    let account_storage_usage = if ProtocolFeature::FixDeleteAccountGlobalContractStorageUsage
        .enabled(current_protocol_version)
    {
        let contract_storage = get_contract_storage_usage(state_update, account_id, account_ref)?;
        account_ref.storage_usage().saturating_sub(contract_storage)
    } else {
        // Legacy behavior: only subtracts local contract code, misses the
        // global contract identifier overhead.
        let account_storage_usage = account_ref.storage_usage();
        let code_len = get_code_len_or_default(
            state_update,
            account_id.clone(),
            account_ref.local_contract_hash().unwrap_or_default(),
        )?;
        debug_assert!(
            code_len == 0 || account_storage_usage > code_len,
            "account storage usage should be larger than code size. storage usage: {}, code size: {}",
            account_storage_usage,
            code_len
        );
        account_storage_usage.saturating_sub(code_len)
    };
    if account_storage_usage > Account::MAX_ACCOUNT_DELETION_STORAGE_USAGE {
        result.result =
            Err(ActionErrorKind::DeleteAccountWithLargeState { account_id: account_id.clone() }
                .into());
        return Ok(());
    }
```

**File:** runtime/runtime/src/actions.rs (L371-371)
```rust
    let remove_result = remove_account(state_update, account_id)?;
```
