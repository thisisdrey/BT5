### Title
Account-deletion state cleanup omits `PostponedReceipt`/`PostponedReceiptId`/`PendingDataCount`, letting a postponed `AddKey` receipt fire against an unrelated recreated account - (`core/store/src/utils/mod.rs`, `runtime/runtime/src/actions.rs`, `runtime/runtime/src/lib.rs`)

### Summary
`remove_account` only clears `Account`, `ContractCode`, `AccessKey`/`GasKeyNonce`, and `ContractData` trie entries, but never clears `TrieKey::PostponedReceipt`, `TrieKey::PostponedReceiptId`, or `TrieKey::PendingDataCount` for the deleted account. If an attacker leaves a postponed self-targeted `ActionReceipt` (e.g. containing `AddKey{full_access}`) pending on their own account, deletes the account, and a third party recreates an account with the identical `account_id`, the attacker can later resolve the pending data dependency and have the stale receipt execute its `AddKey` action against the new owner's account.

### Finding Description
`remove_account` (`core/store/src/utils/mod.rs:505-575`) removes `TrieKey::Account`, `TrieKey::ContractCode`, all `TrieKey::AccessKey`/`TrieKey::gas_key_nonce` entries, and `TrieKey::ContractData` entries, but it never touches `TrieKey::PostponedReceipt`, `TrieKey::PostponedReceiptId`, or `TrieKey::PendingDataCount` [1](#0-0) .

`action_delete_account` (`runtime/runtime/src/actions.rs:314-390`) calls `remove_account` unconditionally as long as `check_actor_permissions` passed [2](#0-1) . `check_actor_permissions` for `DeleteAccount` only requires `actor_id == account_id` and `account.locked() == 0` — it performs no check for outstanding postponed receipts or pending data dependencies tied to the account [3](#0-2) .

A postponed receipt is created in `process_action_receipt` (`runtime/runtime/src/lib.rs:1593-1658`): if any `input_data_ids` are missing, a `TrieKey::PostponedReceiptId{receiver_id, data_id}` link, a `TrieKey::PendingDataCount{receiver_id, receipt_id}` counter, and the receipt itself under `TrieKey::PostponedReceipt{receiver_id, receipt_id}` are written into state [4](#0-3) . All three keys are scoped only by `AccountId` + hash, with no binding to the account's "identity"/creation nonce.

When the missing `DataReceipt` finally arrives, `process_receipt`'s `Data` arm (`runtime/runtime/src/lib.rs:1386-1455`) looks up `TrieKey::PostponedReceiptId` by `(receiver_id, data_id)`, decrements `PendingDataCount`, and once it hits zero, fetches the stored receipt via `get_postponed_receipt` and directly calls `apply_action_receipt` on it [5](#0-4) . Critically, `apply_action_receipt` re-derives `actor_id` fresh from the stored receipt's own `predecessor_id`, not from any live/contextual state: `let mut actor_id = receipt.predecessor_id().clone();` [6](#0-5) . Because the attacker crafted the second-hop receipt with `predecessor_id == receiver_id == attacker_account`, `check_actor_permissions` for the embedded `AddKey` action (which requires `actor_id == account_id`) is satisfied trivially and unconditionally — it says nothing about who currently controls/owns that `account_id` in the trie [7](#0-6) .

Exploit flow:
1. Attacker's contract issues `FunctionCall(receiver=self)` that creates a promise, chained via `promise_then` into a second `ActionReceipt{predecessor_id=self, receiver_id=self, actions=[AddKey{full_access}]}` with an `input_data_id` pending on the first call's result.
2. The contract is designed to delay producing that result indefinitely (only answering when the attacker chooses), so the second receipt sits as a `PostponedReceipt`/`PostponedReceiptId`/`PendingDataCount` triple in state.
3. Attacker submits `DeleteAccount` on the same account; `locked == 0` so `check_actor_permissions` allows it; `action_delete_account` → `remove_account` deletes `Account`/keys/`ContractData` but leaves the postponed-receipt bookkeeping untouched.
4. A third party submits `CreateAccount` with the identical `account_id` (now legitimately unclaimed) and adds their own key(s).
5. Attacker triggers the delayed `DataReceipt` to finally resolve. `process_receipt`'s Data arm still finds the stale `PostponedReceiptId`/`PendingDataCount`/`PostponedReceipt` entries (never cleaned up) and executes the stored `AddKey{full_access}` receipt against the *new* account, because `check_actor_permissions` only compares string-equal `actor_id`/`account_id`, not real ownership.

This breaks the authorization-exactness invariant: a promise action created by the old (attacker) owner ends up executing privileged actions on the new owner's account without their consent.

### Impact Explanation
This is authorization escalation across accounts: the attacker obtains a full-access key on an account it no longer owns, enabling theft of any funds the new legitimate owner deposits into that account, and full compromise of contract state/keys. This matches "authorization escalation across accounts or promises" enabling fund theft in the bounty categories.

### Likelihood Explanation
Preconditions are all attacker-controlled and require no privileged access: deploying an ordinary contract capable of delaying a promise's resolution, chaining a `promise_then` self-call with `AddKey`, and later submitting `DeleteAccount` and the resolving call — all via standard signed transactions to public RPC. The only external dependency is a third party recreating an account with the exact same `account_id` after deletion, which the attacker can also arrange to control (e.g. having a colluding/duped party create the account, or simply choosing an account_id likely to be reused) or that a victim genuinely re-registers. Cost is limited to normal gas/storage fees; the attack is repeatable for any account the attacker owns and later abandons.

### Recommendation
When deleting an account, also purge any `TrieKey::PostponedReceipt`, `TrieKey::PostponedReceiptId`, and `TrieKey::PendingDataCount` entries scoped to that `account_id` (iterate/prefix-scan these columns the same way access keys and contract data are cleaned in `remove_account`), or reject `DeleteAccount` while such pending entries exist for the account. Additionally, `apply_action_receipt`'s `actor_id` derivation should not blindly trust a persisted receipt's `predecessor_id` as authorization proof once the account identity may have changed; consider binding postponed-receipt state to an account "generation"/creation marker that is invalidated on deletion.

### Proof of Concept
Runtime/integration test plan (`runtime/runtime/src/tests/apply.rs` style, similar to `test_function_call_after_same_chunk_delete_recreate_resolves_fresh_code`):
1. Create account `victim.near`, then apply an incoming `ActionReceipt` to it with `input_data_ids: [D]` and `actions: [AddKey{full_access, attacker_key}]`, `predecessor_id == receiver_id == victim.near` — assert it becomes a `PostponedReceipt`/`PendingDataCount`/`PostponedReceiptId` triple in state (no `DataReceipt` for `D` yet).
2. Apply a `DeleteAccount` receipt for `victim.near` (locked == 0) with a benign beneficiary; assert `get_account` returns `None`, but `get_postponed_receipt`, `PostponedReceiptId`, and `PendingDataCount` for that account/receipt/data_id are still present (this proves the `remove_account` asymmetry).
3. Apply a `CreateAccount` receipt for `victim.near` from an unrelated predecessor (simulating a third party) plus its own `AddKey` with a different, legitimate key.
4. Apply the pending `DataReceipt{receiver_id: victim.near, data_id: D}`; assert the postponed receipt executes and the account's access keys now include the attacker's full-access key that was never authorized by the new owner.

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

**File:** runtime/runtime/src/actions.rs (L371-371)
```rust
    let remove_result = remove_account(state_update, account_id)?;
```

**File:** runtime/runtime/src/actions.rs (L739-760)
```rust
pub(crate) fn check_actor_permissions(
    action: &Action,
    account: &Option<Account>,
    actor_id: &AccountId,
    account_id: &AccountId,
) -> Result<(), ActionError> {
    match action {
        Action::DeployContract(_)
        | Action::Stake(_)
        | Action::AddKey(_)
        | Action::DeleteKey(_)
        | Action::DeployGlobalContract(_)
        | Action::UseGlobalContract(_)
        | Action::WithdrawFromGasKey(_) => {
            if actor_id != account_id {
                return Err(ActionErrorKind::ActorNoPermission {
                    account_id: account_id.clone(),
                    actor_id: actor_id.clone(),
                }
                .into());
            }
        }
```

**File:** runtime/runtime/src/actions.rs (L761-776)
```rust
        Action::DeleteAccount(_) => {
            if actor_id != account_id {
                return Err(ActionErrorKind::ActorNoPermission {
                    account_id: account_id.clone(),
                    actor_id: actor_id.clone(),
                }
                .into());
            }
            let account = account.as_ref().unwrap();
            if !account.locked().is_zero() {
                return Err(ActionErrorKind::DeleteAccountStaking {
                    account_id: account_id.clone(),
                }
                .into());
            }
        }
```

**File:** runtime/runtime/src/lib.rs (L855-855)
```rust
        let mut actor_id = receipt.predecessor_id().clone();
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
