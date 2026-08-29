### Title
Deleted account's `PromiseYieldReceipt` state survives `remove_account` and is executed against a same-named recreated account via the shard-global `PromiseYieldTimeout` queue - ([File: runtime/runtime/src/lib.rs], [File: core/store/src/utils/mod.rs])

### Summary
`remove_account` deletes an account's `Account`, `ContractCode`, access/gas keys and `ContractData` entries, but never removes the account's `PromiseYieldReceipt`, `PromiseYieldStatus`, or `YieldIdToDataId`/`DataIdToYieldId` trie rows. Because the timeout queue (`TrieKey::PromiseYieldTimeout{index}`) is indexed by a shard-global monotonic counter rather than by account name, it is completely unaffected by `DeleteAccount`, so a previously-planted yield still fires and, finding the stale `PromiseYieldReceipt` still present under the (now reused) account name, dispatches a `PromiseResume{data:None}` that executes the old, unrelated actions against whatever account currently holds that name.

### Finding Description
`resolve_promise_yield_timeouts` pops `PromiseYieldTimeout` queue entries once `apply_state.block_height` passes `expires_at`, and for each entry checks only whether a `PromiseYieldReceipt` under `queue_entry.account_id`/`queue_entry.data_id` still exists in the trie: [1](#0-0) 

That check calls `state_update.contains_key(&promise_yield_key)`, where `promise_yield_key = TrieKey::PromiseYieldReceipt{receiver_id, data_id}`. This key is per-account (`get_account_id` maps `PromiseYieldReceipt` back to `receiver_id`): [2](#0-1) [3](#0-2) 

However, `remove_account` (invoked by `action_delete_account`) only clears `Account`, `ContractCode`, access/gas keys, and `ContractData` — it never scans/removes `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, or `DataIdToYieldId` entries for the deleted account: [4](#0-3) [5](#0-4) 

Meanwhile, the `PromiseYieldTimeout` queue key is `col::PROMISE_YIELD_TIMEOUT || index` — a shard-global sequence counter with no reference to any account, so nothing about `DeleteAccount` invalidates or clears these queued timeout entries: [6](#0-5) 

As a result: an attacker's contract calls `promise_yield_create` (creating a self-targeted `PromiseYieldReceipt` whose action list is populated via subsequent `promise_batch_action_*` calls on the returned promise index, exactly as ordinary receipts are built through `ReceiptManager::append_action`) and stages a long `timeout_length_in_blocks`. The attacker then deletes the account with `DeleteAccountAction`, which calls `action_delete_account` → `remove_account`, wiping the `Account`/keys/`ContractData` but leaving the `PromiseYieldReceipt` (and its associated `PromiseYieldStatus`) rows intact under the account name. If the account name is later recreated (e.g. a subaccount reused by a different key holder), when the untouched `PromiseYieldTimeout` queue entry eventually expires, `resolve_promise_yield_timeouts` finds the surviving `PromiseYieldReceipt`, builds a `PromiseResume{data: None}` receipt targeting `queue_entry.account_id`, and `apply_action_receipt` executes the original (pre-deletion) action list — e.g. `DeleteKey`/`AddKey` actions — against the state of whoever now owns that account name, since these self-referential actions only check `actor_id == account_id`, which is trivially true because the receipt's `receiver_id`/`predecessor_id` is the account name itself, not any live cryptographic authorization from the current key holder: [7](#0-6) 

None of the existing signature, nonce, or access-key checks apply here because this is a purely internal receipt replay path — the original action list was authorized once, at creation time, under the *old* owner's key, and is never re-validated against the *current* key holder before execution.

### Impact Explanation
This is an authorization-escalation / persistent-account-takeover primitive: actions crafted and authorized by a departing account owner (or a malicious actor who briefly controlled a name) execute later against whatever new owner subsequently holds the same account name, without that new owner's consent — matching the "authorization escalation across accounts or promises" bounty category. Each planted `promise_yield_create` becomes an independent time-delayed privileged-action execution slot, repeatable across as many delete/recreate cycles as the attacker can arrange.

### Likelihood Explanation
Requires only unprivileged transactions: `FunctionCall` invoking `promise_yield_create` + `promise_batch_action_*` to populate the yield's actions, followed by `DeleteAccount`. Exploitability against a truly independent "new owner" further requires that the same account name be re-created for a different key holder after deletion — realistic for subaccount-issuing services (wallets, marketplaces) that recycle freed subaccount names, but not universal to every account. This precondition on name-reuse-by-a-different-party is what limits the likelihood; the underlying state-hygiene defect (stale `PromiseYieldReceipt`/`PromiseYieldStatus`/yield-id-mapping rows surviving `remove_account`) is unconditionally present and reachable by any user account deleting itself while a yield is outstanding.

### Recommendation
In `remove_account` (`core/store/src/utils/mod.rs`), enumerate and remove all `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, and `DataIdToYieldId` trie entries for the account being deleted (mirroring the existing access-key/contract-data prefix-iteration cleanup), and/or have `resolve_promise_yield_timeouts` verify the target account still exists / matches the account epoch that created the yield before re-dispatching the stored action list.

### Proof of Concept
Test-loop integration test in `test-loop-tests/src/tests/yield_timeouts.rs`-style harness:
1. Create subaccount `victim.parent.near` with owner key A; deploy `rs_contract`.
2. Call `call_yield_create_return_promise` twice with different `timeout_length_in_blocks`, batching `action_add_key`/`action_delete_key` actions onto each yield's promise index so the callback would mutate the access-key set.
3. Submit `DeleteAccountAction` for `victim.parent.near` (beneficiary = parent).
4. From `parent.near`, submit `CreateAccountAction` for `victim.parent.near` again with a brand-new owner key B (representing an unrelated new owner), with no interaction from key A.
5. Advance blocks past each yield's `expires_at`.
6. Assert that after each timeout fires, the new account's access-key list (owned by key B) is mutated (e.g., key B removed / an attacker key added) even though key B never authorized or was even aware of the original yields — demonstrating the stale `PromiseYieldReceipt` executed against the wrong owner.

### Citations

**File:** runtime/runtime/src/lib.rs (L1547-1562)
```rust
                    // Execute the PromiseYield receipt. It will read the input data and clean it
                    // up from the state.
                    return self
                        .apply_action_receipt(
                            state_update,
                            apply_state,
                            pipeline_manager,
                            &yield_receipt,
                            receipt_sink,
                            instant_receipts,
                            validator_proposals,
                            stats,
                            epoch_info_provider,
                            receipt_to_tx,
                        )
                        .map(Some);
```

**File:** runtime/runtime/src/lib.rs (L3029-3068)
```rust

        let queue_entry_key =
            TrieKey::PromiseYieldTimeout { index: promise_yield_indices.first_index };

        let queue_entry =
            get::<PromiseYieldTimeout>(state_update, &queue_entry_key)?.ok_or_else(|| {
                StorageError::StorageInconsistentState(format!(
                    "PromiseYield timeout queue entry #{} should be in the state",
                    promise_yield_indices.first_index
                ))
            })?;

        // Queue entries are ordered by expires_at
        if queue_entry.expires_at > apply_state.block_height {
            break;
        }

        // Check if the yielded promise still needs to be resolved
        let promise_yield_key = TrieKey::PromiseYieldReceipt {
            receiver_id: queue_entry.account_id.clone(),
            data_id: queue_entry.data_id,
        };
        if state_update.contains_key(&promise_yield_key, AccessOptions::DEFAULT)? {
            let new_receipt_id = create_receipt_id_from_receipt_id(
                &queue_entry.data_id,
                apply_state.block_height,
                new_receipt_index,
            );
            new_receipt_index += 1;

            // Create a PromiseResume receipt to resolve the timed-out yield.
            let resume_receipt = Receipt::V0(ReceiptV0 {
                predecessor_id: queue_entry.account_id.clone(),
                receiver_id: queue_entry.account_id.clone(),
                receipt_id: new_receipt_id,
                receipt: ReceiptEnum::PromiseResume(DataReceipt {
                    data_id: queue_entry.data_id,
                    data: None,
                }),
            });
```

**File:** core/primitives/src/trie_key.rs (L515-518)
```rust
            TrieKey::PromiseYieldTimeout { index } => {
                buf.push(col::PROMISE_YIELD_TIMEOUT);
                buf.extend(&index.to_le_bytes());
            }
```

**File:** core/primitives/src/trie_key.rs (L519-524)
```rust
            TrieKey::PromiseYieldReceipt { receiver_id, data_id } => {
                buf.push(col::PROMISE_YIELD_RECEIPT);
                buf.extend(receiver_id.as_bytes());
                buf.push(ACCOUNT_DATA_SEPARATOR);
                buf.extend(data_id.as_ref());
            }
```

**File:** core/primitives/src/trie_key.rs (L605-605)
```rust
            TrieKey::PromiseYieldReceipt { receiver_id, .. } => Some(receiver_id.clone()),
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

**File:** runtime/runtime/src/actions.rs (L364-389)
```rust
    // We use current amount as a pay out to beneficiary.
    let account_balance = account_ref.amount();
    if account_balance > Balance::ZERO {
        result
            .new_receipts
            .push(Receipt::new_balance_refund(&delete_account.beneficiary_id, account_balance));
    }
    let remove_result = remove_account(state_update, account_id)?;
    result.tokens_burnt =
        result.tokens_burnt.checked_add(gas_key_balance_to_burn).ok_or_else(|| {
            StorageError::StorageInconsistentState("tokens_burnt overflow".to_string())
        })?;
    if remove_result.gas_key_nonce_count > 0 {
        let compute = storage_removes_compute(
            &config.wasm_config.ext_costs,
            remove_result.gas_key_nonce_count,
            remove_result.gas_key_nonce_total_key_bytes,
            AccessKey::NONCE_VALUE_LEN * remove_result.gas_key_nonce_count,
        );
        result.compute_usage = safe_add_compute(result.compute_usage, compute).map_err(|_| {
            StorageError::StorageInconsistentState("compute_usage overflow".to_string())
        })?;
    }
    *actor_id = receipt.predecessor_id().clone();
    *account = None;
    Ok(())
```
