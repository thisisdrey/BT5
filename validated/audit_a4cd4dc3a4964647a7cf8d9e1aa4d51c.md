### Title
Deleted account's postponed action receipts survive `remove_account` and execute with owner privilege against a re-created account of the same name - (File: core/store/src/utils/mod.rs)

### Summary
`remove_account` (`core/store/src/utils/mod.rs:505-575`) only clears `TrieKey::Account`, `ContractCode`, `AccessKey`/`GasKeyNonce`, and `ContractData` rows, but never touches `TrieKey::PostponedReceipt`, `TrieKey::PostponedReceiptId`, or `TrieKey::PendingDataCount` written by `process_action_receipt` (`runtime/runtime/src/lib.rs:1593-1658`). An account that creates a multi-dependency, self-targeted promise and is then deleted before all its input data receipts arrive leaves a postponed receipt keyed to its own account name in the trie; when the account name is re-created and the remaining `DataReceipt`s are delivered, `process_receipt` (`runtime/runtime/src/lib.rs:1367-1455`) fetches the stale postponed receipt and executes its actions against the newly created account.

### Finding Description
`process_action_receipt` writes three account-keyed trie rows when an `ActionReceipt` has unmet `input_data_ids`: `TrieKey::PostponedReceiptId` per pending `data_id`, `TrieKey::PendingDataCount`, and the serialized receipt itself via `set_postponed_receipt` under `TrieKey::PostponedReceipt` [1](#0-0) . All three keys are namespaced by `receiver_id` (the account name), not by any incarnation/lifetime identifier of the account.

`remove_account`, invoked from `action_delete_account` during `DeleteAccount` processing, removes `Account`, `ContractCode`, all `AccessKey`/`GasKeyNonce` entries, and `ContractData`, but contains no logic to enumerate or remove `PostponedReceipt`, `PostponedReceiptId`, or `PendingDataCount` rows for the account [2](#0-1) . This is a genuine asymmetry: the writer (`process_action_receipt`) and the eraser (`remove_account`) operate on different subsets of account-scoped `TrieKey` variants.

Once the account name is re-created (a fresh `Account` row under the same `TrieKey::Account { account_id }`), the surviving `PostponedReceiptId`/`PendingDataCount`/`PostponedReceipt` entries are still addressable under that same `account_id`. When the outstanding `DataReceipt`s are subsequently delivered, `process_receipt`'s `VersionedReceiptEnum::Data` branch looks up `TrieKey::PostponedReceiptId` by `(account_id, data_id)`, finds the stale entry, decrements `PendingDataCount`, and on the last dependency calls `get_postponed_receipt`/`apply_action_receipt` on the old, now-orphaned `ActionReceipt` [3](#0-2) . Because the original receipt was constructed with `predecessor_id == receiver_id == A` (a self-call), the actions inside it (e.g., `AddKey(FullAccess)`) execute against whatever account now exists at `A`, passing `check_actor_permissions`'s self-call check even though no `AddKey` transaction was ever validated against the new account's own state.

### Impact Explanation
This is an authorization-escalation / account-takeover primitive: a completely unrelated (freshly re-created) account inherits privileged actions (e.g., a `FullAccess` access key) that were only ever authorized by the account's prior incarnation. This falls under NEAR's "authorization escalation across accounts" bounty category and can lead to loss of control over, or draining of, the re-created account by whoever crafted the original promise.

### Likelihood Explanation
The attack requires only: (1) a funded sub-account, (2) the ability to deploy a wasm contract, and (3) knowledge of the account's own predictable receipt IDs / promise construction (all standard, permissionless operations). The sequence — create A, have A schedule a two-dependency self-promise carrying `AddKey(FullAccess)`, deliver one data receipt, `DeleteAccount(A)`, `CreateAccount(A)`, deliver the second data receipt — is fully reproducible and repeatable by any attacker controlling account `A`'s contract logic, with no validator/node privilege required.

### Recommendation
Extend `remove_account` (or add a dedicated cleanup pass called from `action_delete_account`) to enumerate and remove all `TrieKey::PostponedReceipt`, `TrieKey::PostponedReceiptId`, and `TrieKey::PendingDataCount` entries prefixed by the deleted `account_id` before the account is torn down, mirroring the existing prefix-iteration pattern already used for access keys and contract data in `remove_account`. Alternatively/additionally, reject `DeleteAccount` while the account has any outstanding postponed receipts, or persist an account "incarnation" counter that is mixed into these trie keys so a re-created account can never observe rows written by a prior incarnation.

### Proof of Concept
Integration test (apply-path, e.g. under `runtime/runtime/src/tests` or `integration-tests` using the runtime test-loop harness):
1. Create account `A` with a contract that, on a designated entry point, issues a `promise_batch_action_function_call` targeting itself twice (or `promise_and` of two cross-contract calls) followed by `promise_batch_action_add_key_with_full_access` attached to the joined promise, guaranteeing `predecessor_id == receiver_id == A` on the resulting `ActionReceipt` and two `input_data_ids`.
2. Apply the chunk so `process_action_receipt` postpones the receipt, asserting via direct trie reads that `TrieKey::PostponedReceipt { receiver_id: A, .. }`, `TrieKey::PostponedReceiptId`, and `TrieKey::PendingDataCount` exist.
3. Deliver only one of the two `DataReceipt`s (apply a chunk containing it), leaving the postponed state intact.
4. Submit and apply a `DeleteAccount(A, beneficiary)` transaction; assert `TrieKey::Account { A }` is gone but the three postponed-receipt keys above still return `Some(..)` from the trie.
5. Submit and apply `CreateAccount(A)` with a fresh key.
6. Deliver the second `DataReceipt`; apply the chunk.
7. Assert that account `A`'s access-key set now contains the `FullAccess` key from the original promise, despite no `AddKey` transaction ever being signed against the new account's own keys — confirming the escalation.

### Citations

**File:** runtime/runtime/src/lib.rs (L1396-1455)
```rust
                // given data_id.
                // If we don't have a postponed receipt yet, we don't need to do anything for now.
                if let Some(receipt_id) = get(
                    state_update,
                    &TrieKey::PostponedReceiptId {
                        receiver_id: account_id.clone(),
                        data_id: data_receipt.data_id,
                    },
                )? {
                    // There is already a receipt that is awaiting for the just received data.
                    // Removing this pending data_id for the receipt from the state.
                    state_update.remove(TrieKey::PostponedReceiptId {
                        receiver_id: account_id.clone(),
                        data_id: data_receipt.data_id,
                    });
                    // Checking how many input data items is pending for the receipt.
                    let pending_data_count: u32 = get(
                        state_update,
                        &TrieKey::PendingDataCount { receiver_id: account_id.clone(), receipt_id },
                    )?
                    .ok_or_else(|| {
                        StorageError::StorageInconsistentState(
                            "pending data count should be in the state".to_string(),
                        )
                    })?;
                    if pending_data_count == 1 {
                        // It was the last input data pending for this receipt. We'll cleanup
                        // some receipt related fields from the state and execute the receipt.

                        // Removing pending data count from the state.
                        state_update.remove(TrieKey::PendingDataCount {
                            receiver_id: account_id.clone(),
                            receipt_id,
                        });
                        // Fetching the receipt itself.
                        let ready_receipt =
                            get_postponed_receipt(state_update, account_id, receipt_id)?
                                .ok_or_else(|| {
                                    StorageError::StorageInconsistentState(
                                        "pending receipt should be in the state".to_string(),
                                    )
                                })?;
                        // Removing the receipt from the state.
                        remove_postponed_receipt(state_update, account_id, receipt_id);
                        // Executing the receipt. It will read all the input data and clean it up
                        // from the state.
                        return self
                            .apply_action_receipt(
                                state_update,
                                apply_state,
                                pipeline_manager,
                                &ready_receipt,
                                receipt_sink,
                                instant_receipts,
                                validator_proposals,
                                stats,
                                epoch_info_provider,
                                receipt_to_tx,
                            )
                            .map(Some);
```

**File:** runtime/runtime/src/lib.rs (L1608-1655)
```rust
        let mut pending_data_count: u32 = 0;
        for data_id in action_receipt.input_data_ids() {
            if !has_received_data(state_update, account_id, *data_id)? {
                pending_data_count += 1;
                // The data for a given data_id is not available, so we save a link to this
                // receipt_id for the pending data_id into the state.
                set(
                    state_update,
                    TrieKey::PostponedReceiptId {
                        receiver_id: account_id.clone(),
                        data_id: *data_id,
                    },
                    receipt.receipt_id(),
                )
            }
        }

        if pending_data_count == 0 {
            // All input data is available. Executing the receipt. It will cleanup
            // input data from the state.
            return self
                .apply_action_receipt(
                    state_update,
                    apply_state,
                    pipeline_manager,
                    receipt,
                    receipt_sink,
                    instant_receipts,
                    validator_proposals,
                    stats,
                    epoch_info_provider,
                    receipt_to_tx,
                )
                .map(Some);
        } else {
            // Not all input data is available now.
            // Save the counter for the number of pending input data items into the state.
            set(
                state_update,
                TrieKey::PendingDataCount {
                    receiver_id: account_id.clone(),
                    receipt_id: *receipt.receipt_id(),
                },
                &pending_data_count,
            );
            // Save the receipt itself into the state.
            set_postponed_receipt(state_update, receipt);
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
