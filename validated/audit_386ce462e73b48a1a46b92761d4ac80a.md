### Title
Stale `PostponedReceipt`/`PendingDataCount`/`PostponedReceiptId` entries survive `DeleteAccount` and are executed against a recreated account, enabling authorization escalation across account lifetimes - ([File: runtime/runtime/src/lib.rs, core/store/src/utils/mod.rs])

### Summary
`remove_account` in `core/store/src/utils/mod.rs` deletes the `Account`, `ContractCode`, access-key/gas-key, and `ContractData` trie entries for a deleted account, but never touches `PostponedReceipt`, `PostponedReceiptId`, `PendingDataCount`, or `ReceivedData` entries. [1](#0-0)  If an account has an outstanding multi-input-data `ActionReceipt` postponed on it when `DeleteAccount` executes, that postponed state remains in the trie after the account row is gone, and fires when a later `Data` receipt (resolving the original, still-outstanding promise) arrives - potentially against a completely different, newly (re)created account of the same name.

### Finding Description
The `Data` branch of `process_receipt` looks up `TrieKey::PostponedReceiptId{receiver_id, data_id}` purely by `(receiver_id, data_id)`, decrements `TrieKey::PendingDataCount{receiver_id, receipt_id}`, and when it hits zero, fetches and executes the stored `TrieKey::PostponedReceipt{receiver_id, receipt_id}` via `apply_action_receipt`. [2](#0-1)  None of this logic checks that the account the postponed receipt was created under is the "same" account instance still holding the state — it only checks trie-key existence.

`remove_account`, invoked by the `DeleteAccount` action handler, removes the account record, code, access keys, gas keys, and contract data, but has no code path removing `PostponedReceipt`, `PostponedReceiptId`, `PendingDataCount`, or `ReceivedData` entries scoped to the account. [3](#0-2)  These are keyed by `receiver_id`/`receipt_id`/`data_id`, entirely independent of the account's existence, so they persist in the trie exactly as before deletion.

Exploit flow:
1. Attacker creates and controls `x.near`.
2. Attacker triggers a receipt to `x.near` that depends on two input data ids (e.g. `Promise::and` joining two cross-contract calls), with an action list attached that performs self-authorized actions (`AddKey`, `Transfer`, `DeployContract`, etc. — actions that require no external signature check at execution time because the receipt's receiver is executing them against itself).
3. One of the two data receipts arrives, resolving into a postponed `ActionReceipt` + `PendingDataCount = 1` + a remaining `PostponedReceiptId` waiting on the second, still-outstanding data id.
4. Attacker deletes `x.near` via `DeleteAccount` before the second data receipt is delivered. `remove_account` clears the account/keys/code but leaves the postponed-receipt bookkeeping intact.
5. `x.near` is now unowned; a victim later registers a fresh `x.near`, funds it, and deploys their own contract.
6. The still-in-flight second `Data` receipt (from the original promise chain, delivered on its own schedule, independent of account existence) is routed to `x.near`, matches the surviving `PostponedReceiptId`, drives `PendingDataCount` to zero, and `apply_action_receipt` executes the attacker's original postponed action list against the victim's brand-new account.

No existing check in this path validates that the account instance is the same one the postponed receipt was created for, and no nonce/signature/access-key check applies to self-actions executed via a postponed `ActionReceipt`, since those checks were already performed (and passed) at the time the original receipt was created against the original `x.near`.

### Impact Explanation
This allows an attacker's previously-authorized delayed action (e.g., adding a full-access key, transferring funds out, or deploying arbitrary contract code) to execute against an unrelated victim account that later reuses the same account id — an authorization escalation across accounts/promises, matching the "authorization escalation across accounts or promises" bounty category. In the worst case it results in theft of the victim's funds or full compromise of the victim's freshly-deployed contract.

### Likelihood Explanation
The attacker needs only ordinary account/tx capabilities: create/own `x.near`, issue a self-targeted multi-dependency receipt, delete the account at the right moment, and wait for a third party to reuse the account name (reuse of deleted named accounts is standard and expected). The main uncertainty is the timing dependency (a victim must recreate the exact same account id before the delayed data receipt resolves), which the attacker can extend by chaining extra cross-contract/cross-shard hops to control the delay window, making the attack feasible though timing-dependent and not instantaneously guaranteed. It is repeatable per victim account name and does not require any validator, node, or network-level privilege.

### Recommendation
`remove_account` (or the `DeleteAccount` action handler) should also purge any `PostponedReceipt`, `PostponedReceiptId`, `PendingDataCount`, and orphaned `ReceivedData`/`PromiseYieldReceipt`/`PromiseYieldStatus` entries scoped to the account being deleted — or `DeleteAccount` should be disallowed while such pending postponed-receipt state exists for the account (mirroring how it already restricts deletion based on other outstanding account state).

### Proof of Concept
Runtime apply-path integration test:
1. Deploy a contract on `x.near` that, on call, issues two cross-contract promises joined with `promise_and`/`then`, so a 2-input-data `ActionReceipt` gets postponed on `x.near` with an attached malicious action (e.g. `AddKey` for an attacker-controlled key).
2. Apply the chunk delivering the first `Data` receipt — assert `PendingDataCount == 1` and `PostponedReceiptId`/`PostponedReceipt` exist in state for `x.near`.
3. In the next chunk, apply a `DeleteAccount` receipt for `x.near`. Assert the account row is gone but `PostponedReceipt`/`PostponedReceiptId`/`PendingDataCount` keys are still readable from `state_update`.
4. Apply a `CreateAccount` (+`Transfer`) receipt recreating `x.near` as a fresh account controlled by a different (victim) key.
5. Apply the second, originally outstanding `Data` receipt for the same `data_id`.
6. Assert that `apply_action_receipt` executes the attacker's originally postponed action (e.g. the attacker's `AddKey` appears on the victim's new `x.near` account), demonstrating cross-account authorization escalation.

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

**File:** runtime/runtime/src/lib.rs (L1398-1455)
```rust
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
