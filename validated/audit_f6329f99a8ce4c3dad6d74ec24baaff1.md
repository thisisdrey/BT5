### Title
Postponed `ActionReceipt` rows (including attacker-chosen `DeployContractAction`) survive `remove_account` and later execute onto a freshly re-created account - ([File: core/store/src/utils/mod.rs])

### Summary
`remove_account` clears `Account`, `ContractCode`, `AccessKey`/`GasKeyNonce`, and `ContractData` rows for a deleted account, but never touches the `PostponedReceipt`, `PostponedReceiptId`, or `PendingDataCount` trie rows keyed by that account's `receiver_id`. If an attacker-controlled account `A` has a self-directed postponed `ActionReceipt` (predecessor_id == receiver_id == `A`) containing a `DeployContractAction` waiting on missing input data, deleting `A` and letting someone else re-create `A` does not purge that pending receipt. When the missing data later arrives, `process_receipt`/`process_action_receipt` will still locate and execute the stale receipt against the new account, deploying the attacker's old bytecode without the new owner's consent.

### Finding Description
`remove_account` in `core/store/src/utils/mod.rs` explicitly removes only these categories of state for the deleted account: [1](#0-0) 
It also iterates and removes access keys/gas-key nonces and contract data rows, but there is no removal call for `TrieKey::PostponedReceipt`, `TrieKey::PostponedReceiptId`, or `TrieKey::PendingDataCount`: [2](#0-1) 

These postponed-receipt structures are keyed purely by `receiver_id` + `receipt_id`/`data_id`, with no binding to a specific account "incarnation" (e.g. no dependence on the account's storage nonce, creation block, or any other identity marker that changes on delete/recreate): [3](#0-2) 

The runtime deliberately persists postponed receipts across the row `TrieKey::PostponedReceipt { receiver_id, receipt_id }` set via `set_postponed_receipt`/`get_postponed_receipt`: [4](#0-3) 

The exploit flow:
1. Attacker controls account `A`. `A`'s own contract issues a self-directed `ActionReceipt` (predecessor_id == receiver_id == `A`) containing a `DeployContractAction` with attacker-chosen bytecode, and an `input_data_ids` entry pointing at a `data_id` produced by another promise the attacker also controls (e.g. a helper contract callback).
2. Because `input_data_ids` is not yet satisfied, `process_action_receipt` stores the receipt as postponed and records `PendingDataCount`/`PostponedReceiptId`/`PostponedReceipt` rows keyed by `receiver_id = A`: [5](#0-4) 
3. `validate_deploy_contract_action` only checks the contract-size limit -- it performs no `predecessor_id == receiver_id` re-check at execution time, so the deploy action is accepted purely because it was self-directed at receipt-creation time; nothing re-validates authorization when the postponed receipt is finally applied: [6](#0-5) 
4. Attacker submits a `DeleteAccountAction` for `A`, triggering `remove_account`, which -- as shown above -- leaves the `PostponedReceipt`/`PostponedReceiptId`/`PendingDataCount` rows for `A` intact in the trie.
5. A distinct `CreateAccountAction` transaction recreates `A` (new owner, fresh state, no code deployed).
6. The attacker later completes the still-controlled helper promise so the missing `DataReceipt` with the matching `data_id` is delivered to receiver `A`. `process_receipt`'s `VersionedReceiptEnum::Data` branch looks up `PostponedReceiptId`/`PendingDataCount` purely by `(receiver_id, data_id)`/`(receiver_id, receipt_id)`, finds the stale entries still present, decrements the pending count to zero, fetches the old postponed receipt via `get_postponed_receipt`, and executes it through `apply_action_receipt`: [7](#0-6) 
This deploys the attacker's original bytecode onto the freshly re-created `A`, even though a `CreateAccountAction` was processed in between and the new owner never authorized any deployment.

Why existing checks don't stop it: the original self-call check (predecessor_id == receiver_id) was valid at the time the receipt was *created*, before `A` was deleted; the runtime never re-validates authorization against the account's current identity/owner when a postponed receipt is finally executed, and `remove_account` never invalidates postponed receipts tied to the account being destroyed.

### Impact Explanation
This is a contract-deployment authorization-escalation bug: an attacker can force wasm bytecode of their choosing onto an account that a subsequent, unrelated party legitimately re-created and believes to be empty/uninitialized. This matches the "authorization escalation across accounts or promises" bounty category, since it lets a Receipt authorized under a past account owner mutate the account after a distinct, unrelated `CreateAccountAction` establishes a new owner -- effectively an account/contract takeover vector that could be used to trick a new owner into interacting with attacker-controlled code, or to break assumptions of tooling/users that a freshly created account starts uninitialized.

### Likelihood Explanation
This requires an attacker who can act as the initial owner of account `A` (fully within an ordinary, unprivileged user's capability: any account can send itself an `ActionReceipt` via a self-call, and any account can delete/recreate its own account) plus timing control over when the deferred `DataReceipt` finally arrives (also fully attacker-controlled since it originates from the attacker's own helper contract/promise chain). Account recreation of the same `account_id` by an unrelated third party is a real and expected scenario in NEAR (implicit or named accounts can be deleted and later re-created by anyone). The attack is deterministic and repeatable once state is set up; it costs only ordinary gas/storage fees.

### Recommendation
`remove_account` should also purge any postponed-receipt-related rows tied to the account being deleted before it completes: iterate and remove all `TrieKey::PostponedReceiptId`, `TrieKey::PendingDataCount`, and `TrieKey::PostponedReceipt` entries whose `receiver_id` equals the deleted account (similar to how access keys and contract data are already enumerated and removed), or alternatively bind postponed-receipt identity to an account "epoch"/creation-nonce that is checked before applying a delayed receipt to the current account state.

### Proof of Concept
Integration test in the apply-path test module (e.g. extending `runtime/runtime/src/tests/apply.rs`, which already exercises `DeployContractAction`):
1. Create account `A` with a contract deployed that, on call, issues a self promise (`predecessor_id == receiver_id == A`) containing a `DeployContractAction` with attacker bytecode `code_1`, with `input_data_ids` pointing at a data id produced by a second helper contract `B`'s callback.
2. Apply the receipt so that `TrieKey::PostponedReceipt{receiver_id: A, ..}` is persisted in state (assert via `get_postponed_receipt`).
3. Submit and apply a `DeleteAccountAction` for `A` (beneficiary arbitrary), and assert `TrieKey::Account{A}` is gone but `TrieKey::PostponedReceipt{receiver_id: A, ..}` and `TrieKey::PendingDataCount{receiver_id: A, ..}` are still present in the trie.
4. Submit and apply a `CreateAccountAction` recreating `A` from a different signer (simulating a new, unrelated owner), and assert the new `A` has `AccountContract::None`.
5. Trigger completion of helper contract `B`'s promise so the matching `DataReceipt` (receiver_id = A, data_id matching) is delivered and processed.
6. Assert that after step 5, `AccountContract` of `A` equals the hash of `code_1` (the attacker's pre-deletion bytecode) despite the intervening distinct `CreateAccountAction` -- proving the authorization-escalation across account re-creation.

### Citations

**File:** core/store/src/utils/mod.rs (L120-143)
```rust
pub fn set_postponed_receipt(state_update: &mut TrieUpdate, receipt: &Receipt) {
    assert!(matches!(receipt.versioned_receipt(), VersionedReceiptEnum::Action(_)));
    let key = TrieKey::PostponedReceipt {
        receiver_id: receipt.receiver_id().clone(),
        receipt_id: *receipt.receipt_id(),
    };
    set(state_update, key, receipt);
}

pub fn remove_postponed_receipt(
    state_update: &mut TrieUpdate,
    receiver_id: &AccountId,
    receipt_id: CryptoHash,
) {
    state_update.remove(TrieKey::PostponedReceipt { receiver_id: receiver_id.clone(), receipt_id });
}

pub fn get_postponed_receipt(
    trie: &dyn TrieAccess,
    receiver_id: &AccountId,
    receipt_id: CryptoHash,
) -> Result<Option<Receipt>, StorageError> {
    get(trie, &TrieKey::PostponedReceipt { receiver_id: receiver_id.clone(), receipt_id })
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

**File:** core/store/src/utils/mod.rs (L551-574)
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
    Ok(RemoveAccountResult { gas_key_nonce_count, gas_key_nonce_total_key_bytes })
```

**File:** core/primitives/src/trie_key.rs (L204-219)
```rust
    PostponedReceiptId {
        receiver_id: AccountId,
        data_id: CryptoHash,
    } = col::POSTPONED_RECEIPT_ID,
    /// Used to store the number of still missing input data `u32` for a given receiver's
    /// `AccountId` and a given `receipt_id` of the receipt.
    PendingDataCount {
        receiver_id: AccountId,
        receipt_id: CryptoHash,
    } = col::PENDING_DATA_COUNT,
    /// Used to store the postponed receipt `primitives::receipt::Receipt` for a given receiver's
    /// `AccountId` and a given `receipt_id` of the receipt.
    PostponedReceipt {
        receiver_id: AccountId,
        receipt_id: CryptoHash,
    } = col::POSTPONED_RECEIPT,
```

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

**File:** runtime/runtime/src/action_validation.rs (L223-236)
```rust
/// Validates `DeployContractAction`. Checks that the given contract size doesn't exceed the limit.
fn validate_deploy_contract_action(
    limit_config: &LimitConfig,
    action: &DeployContractAction,
) -> Result<(), ActionsValidationError> {
    if action.code.len() as u64 > limit_config.max_contract_size {
        return Err(ActionsValidationError::ContractSizeExceeded {
            size: action.code.len() as u64,
            limit: limit_config.max_contract_size,
        });
    }

    Ok(())
}
```
