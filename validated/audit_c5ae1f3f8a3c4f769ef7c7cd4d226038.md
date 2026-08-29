### Title
Orphaned `ReceivedData`/`PostponedReceipt`/`PendingDataCount` entries surviving `DeleteAccount` allow a recreated account to hijack a stale cross-shard promise callback - (File: `core/store/src/utils/mod.rs`)

### Summary
`remove_account` only clears `TrieKey::Account`, `TrieKey::ContractCode`, access/gas keys (via `get_raw_prefix_for_access_keys`), and contract storage (via `get_raw_prefix_for_contract_data`) [1](#0-0) . It never removes `TrieKey::ReceivedData`, `TrieKey::PostponedReceipt`, `TrieKey::PostponedReceiptId`, or `TrieKey::PendingDataCount` entries keyed on the account's own `receiver_id`. Because none of these keys are scoped by an account "incarnation"/generation counter, a `DeleteAccount` followed by a same-named `CreateAccount` leaves the old promise-matching state alive so that a later-arriving `DataReceipt` completes/executes a postponed `ActionReceipt` against the newly created account.

### Finding Description
When an `ActionReceipt` with unmet `input_data_ids` reaches an account, the runtime persists `TrieKey::PostponedReceiptId{receiver_id, data_id}`, `TrieKey::PendingDataCount{receiver_id, receipt_id}`, and `TrieKey::PostponedReceipt{receiver_id, receipt_id}` [2](#0-1) . When the matching `DataReceipt` later arrives, the runtime writes `TrieKey::ReceivedData{receiver_id, data_id}`, looks up the `PostponedReceiptId` link, decrements `PendingDataCount`, and — once it hits zero — fetches and executes the stored `PostponedReceipt` [3](#0-2) . All of these keys are namespaced purely by `receiver_id` (the account name), with no notion of "which incarnation of this account name" they belong to.

`remove_account`, invoked by the `DeleteAccount` action, removes only `TrieKey::Account`, `TrieKey::ContractCode`, all access keys/gas-key nonces (prefix `get_raw_prefix_for_access_keys`), and all contract storage (prefix `get_raw_prefix_for_contract_data`) [1](#0-0) . It does not iterate or clear `ReceivedData`, `PostponedReceipt`, `PostponedReceiptId`, or `PendingDataCount` prefixes for that account. There is no check elsewhere in the `DeleteAccount` action path (`runtime/runtime/src/actions.rs`) that rejects deletion while the account has outstanding postponed receipts or pending data.

Exploit flow:
1. Attacker's account `A` triggers a `promise_then`/`promise_and` chain producing an `ActionReceipt` with `input_data_ids` targeting `A`, which arrives at `A`'s shard before the corresponding `DataReceipt` — this is normal async cross-shard timing and gets persisted as a `PostponedReceipt`/`PostponedReceiptId`/`PendingDataCount` under key `A`.
2. Before the `DataReceipt` lands, attacker submits `DeleteAccount` for `A`. `remove_account` wipes the account/keys/code/storage but leaves the postponed-receipt bookkeeping intact.
3. Attacker submits `CreateAccount` (optionally `DeployContract`) recreating `A` with new code/keys.
4. The delayed `DataReceipt` for the old `data_id` arrives, writes `ReceivedData{A, data_id}` into the new account's namespace, finds the surviving `PostponedReceiptId` link, and — once the count reaches zero — executes the old `PostponedReceipt` against the newly created account (its new contract, new keys, new state) [4](#0-3) .

No existing check (signature/nonce/access-key/action validation, storage staking) inspects for outstanding `PostponedReceiptId`/`PendingDataCount`/`PostponedReceipt` entries before permitting `DeleteAccount`, so nothing stops this sequence.

### Impact Explanation
This breaks the determinism/authorization-exactness invariant that a receipt executes against the account instance it was addressed to: a stale postponed `ActionReceipt` (with its original prepaid gas/deposit and `predecessor_id`) instead executes against attacker-controlled code deployed after account recreation. This falls under "authorization escalation across accounts or promises" — an attacker can redirect execution of a receipt (potentially originating from a third-party's promise chain that named the attacker's account as a callback target) into code the attacker deployed only after the original receipt was queued, and can also receive `ReceivedData`/postponed-receipt state that logically belonged to a deleted identity.

### Likelihood Explanation
Preconditions are attacker-controlled and cheap: any account can create a `promise_then`/`promise_and` chain producing a pending cross-shard data dependency on itself, race a `DeleteAccount` transaction before the `DataReceipt` lands, and recreate the account. Timing an action to land between receipt-issuance and cross-shard delivery is feasible but not guaranteed on the first attempt (chunk/shard timing dependent), making it probabilistic rather than deterministic, and repeatable via retries. This is fully reachable through ordinary signed transactions to a public RPC endpoint with no special privileges.

### Recommendation
`remove_account` should also iterate and remove `TrieKey::ReceivedData`, `TrieKey::PostponedReceipt`, `TrieKey::PostponedReceiptId`, and `TrieKey::PendingDataCount` (and `PromiseYieldReceipt`/`PromiseYieldStatus`/`YieldIdToDataId`/`DataIdToYieldId` for completeness) prefixed by the account, using the existing `trie_key_parsers` prefix helpers, before allowing the same account name to be reused; alternatively, `DeleteAccount` should be rejected while any such pending entries exist for the account.

### Proof of Concept
Unit test in `core/store/src/utils/mod.rs` (or an integration test alongside `runtime/runtime/src/tests/apply.rs`):
1. Build a `TrieUpdate`, call `set_postponed_receipt`, `set` for `PostponedReceiptId`/`PendingDataCount`, and `set_received_data` for account `"alice.near"`.
2. Call `remove_account(&mut state_update, &"alice.near".parse().unwrap())`.
3. Assert `get_postponed_receipt`, `get(&TrieKey::PostponedReceiptId{...})`, `get(&TrieKey::PendingDataCount{...})`, and `has_received_data` all still return `Some`/`true` after `remove_account` — demonstrating the entries are never cleared.
4. End-to-end runtime test: `promise_then` from a victim contract into `alice.near` producing a postponed receipt; submit `DeleteAccount` for `alice.near`; submit `CreateAccount`+`DeployContract` for `alice.near` with different wasm; deliver the delayed `DataReceipt`; assert the postponed receipt executes against the new contract/state, confirming cross-incarnation receipt execution.

### Citations

**File:** core/store/src/utils/mod.rs (L505-574)
```rust
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
```

**File:** runtime/runtime/src/lib.rs (L1386-1455)
```rust
            VersionedReceiptEnum::Data(data_receipt) => {
                // Received a new data receipt.
                // Saving the data into the state keyed by the data_id.
                set_received_data(
                    state_update,
                    account_id.clone(),
                    data_receipt.data_id,
                    &ReceivedData { data: data_receipt.data.clone() },
                );
                // Check if there is already a receipt that was postponed and was awaiting for the
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
