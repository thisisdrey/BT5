### Title
`remove_account` fails to clear postponed-receipt state (`PostponedReceiptId`/`PendingDataCount`/`PostponedReceipt`), allowing a self-targeted postponed receipt to execute against a re-created account of the same name - ([File: core/store/src/utils/mod.rs])

### Summary
`remove_account` (invoked by `action_delete_account`) removes `TrieKey::Account`, `TrieKey::ContractCode`, all access/gas keys, and `TrieKey::ContractData`, but never removes `TrieKey::PostponedReceiptId`, `TrieKey::PendingDataCount`, or `TrieKey::PostponedReceipt` rows keyed by the deleted `receiver_id`. If an account deletes itself while it has an outstanding multi-dependency postponed action receipt targeting itself, the postponed-receipt bookkeeping survives the deletion. When the missing `DataReceipt` later arrives, `process_receipt` looks up `PostponedReceiptId`/`PendingDataCount`/`PostponedReceipt` purely by `(receiver_id, data_id/receipt_id)` and executes the stale receipt against whatever account currently occupies that `account_id`, even if a completely different owner has since recreated it.

### Finding Description
An account can construct a self-targeted multi-dependency `ActionReceipt` (e.g. via `promise_and` over two cross-contract calls back to itself, then `.then(...)`), which is a normal, unprivileged pattern. When such a receipt arrives with `receiver_id == predecessor_id` and 2+ `input_data_ids`, `process_action_receipt` (`runtime/runtime/src/lib.rs:1593-1658`) writes:
- `TrieKey::PostponedReceiptId { receiver_id, data_id }` for each missing dependency,
- `TrieKey::PendingDataCount { receiver_id, receipt_id }`,
- `TrieKey::PostponedReceipt { receiver_id, receipt_id }` (via `set_postponed_receipt`). [1](#0-0) 

The account can then submit an ordinary `DeleteAccount` transaction on itself while one dependency is still outstanding; `check_actor_permissions` only rejects `DeleteAccount` if the account has a non-zero locked (staked) balance — it has no check for outstanding postponed receipts. [2](#0-1) 

`action_delete_account` then calls `remove_account`, which iterates and removes `Account`, `ContractCode`, access keys, gas-key nonces, and `ContractData` — but has no logic touching `PostponedReceiptId`, `PendingDataCount`, or `PostponedReceipt`. [3](#0-2) 

The account name can then be recreated (e.g. as a subaccount registered through a registrar-style contract, or reused by whoever is now entitled to create that name) via `action_create_account`, which only checks `account.is_some()`/namespace rules — it has no awareness of leftover postponed-receipt state for that `account_id`. [4](#0-3) 

When the missing `ReceivedData` for the old `data_id`/`receipt_id` finally arrives, `process_receipt` looks up `TrieKey::PostponedReceiptId { receiver_id, data_id }` purely by account-name/data-id, finds the surviving link, decrements `PendingDataCount`, and — once it reaches zero — fetches and executes the surviving `PostponedReceipt` via `apply_action_receipt`, against the account that currently exists under that name (the newly created one), not the deleted one. [5](#0-4) 

Because the postponed receipt's `predecessor_id` equals `receiver_id` (it was a self-call), any privileged action inside it (e.g. `AddKey`) passes `check_actor_permissions`'s `actor_id == account_id` check unconditionally, since `actor_id` is seeded from the receipt's own `predecessor_id`. [6](#0-5) 

This lets an action authorized solely by the *old* incarnation of the account execute with full privilege against the *new* incarnation, without the new owner's access key ever authorizing it.

### Impact Explanation
This is an authorization-escalation bug: a privileged action (`AddKey`, `Transfer`, etc.) created and authorized by the old account owner is later applied to a newly (re)created account of the same name, without the new owner's consent. In the worst case (`AddKey`) this grants the original attacker a full-access key on someone else's account, i.e. authorization escalation across account "incarnations," matching the bounty category of authorization escalation across accounts/promises.

### Likelihood Explanation
Preconditions: the attacker must (1) own a funded account capable of issuing a self-targeted multi-dependency promise (trivial — `promise_and` + `.then()` back to `env::current_account_id()` is standard SDK usage), (2) be able to delay one dependency (trivial by controlling both cross-contract calls or timing), and (3) issue a normal `DeleteAccount` transaction while `PendingDataCount > 0` (no validation blocks this). The harder precondition is getting the account name reused by a different owner (e.g. via a registrar/marketplace contract that permits re-registration of freed subaccount names) — this is plausible for any system that recycles account names, but is not guaranteed for every deployment. The bug itself (unswept trie keys) is deterministic and 100% reproducible with a runtime-apply integration test, independent of whether a "new owner" scenario is realized.

### Recommendation
`remove_account` (or `action_delete_account`) should also enumerate and remove any surviving `PostponedReceiptId`, `PendingDataCount`, and `PostponedReceipt` entries for the account being deleted before removing the account, mirroring how access keys and contract data are swept via prefix iteration. At minimum, `action_delete_account` should refuse to delete an account (or force-cancel/refund) while it has any outstanding `PendingDataCount` entries, analogous to the existing `DeleteAccountStaking` check for locked balance.

### Proof of Concept
Integration/runtime-apply test plan (mirroring `runtime/runtime/src/tests/apply.rs::test_function_call_after_same_chunk_delete_recreate_resolves_fresh_code`, which already demonstrates delete+recreate within one chunk is a supported/tested scenario for code resolution but not for postponed receipts):
1. Deploy a contract to `child.alice.near` and have it emit a self-targeted `ActionReceipt` with `receiver_id == predecessor_id == child.alice.near` and 2 `input_data_ids` (simulating `promise_and`), including an `AddKey` action in the receipt's action list, so `PostponedReceiptId`/`PendingDataCount`/`PostponedReceipt` get written for `child.alice.near`.
2. Submit a `DeleteAccount` receipt for `child.alice.near` (locked balance = 0) in the same or next chunk.
3. Assert, via direct trie inspection (`get`/`get_postponed_receipt` on the committed `TrieUpdate`), that `TrieKey::PostponedReceiptId`, `TrieKey::PendingDataCount`, and `TrieKey::PostponedReceipt` rows for `child.alice.near` are still present after the delete — i.e., survivor count > 0 (currently true; expected to be 0 after fix).
4. Submit `CreateAccount` for `child.alice.near` from `alice.near` (recreating it fresh, with no access keys).
5. Deliver the missing `DataReceipt` for the outstanding `data_id`/`receipt_id`.
6. Assert the postponed `AddKey` action executes and successfully adds a key to the freshly recreated `child.alice.near` account — demonstrating an action authorized only by the deleted incarnation was applied to the new incarnation without any signature/access-key check tied to the new account state.

### Citations

**File:** runtime/runtime/src/lib.rs (L1395-1455)
```rust
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

**File:** runtime/runtime/src/actions.rs (L739-785)
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
        Action::CreateAccount(_)
        | Action::FunctionCall(_)
        | Action::Transfer(_)
        | Action::TransferToGasKey(_) => (),
        Action::Delegate(_) | Action::DelegateV2(_) => (),
        Action::DeterministicStateInit(_) => (),
    };
    Ok(())
}
```

**File:** runtime/runtime/src/actions.rs (L787-818)
```rust
pub(crate) fn check_account_existence(
    action: &Action,
    account: &Option<Account>,
    account_id: &AccountId,
    config: &RuntimeConfig,
    implicit_account_creation_eligible: bool,
) -> Result<(), ActionError> {
    match action {
        Action::CreateAccount(_) => {
            if account.is_some() {
                return Err(ActionErrorKind::AccountAlreadyExists {
                    account_id: account_id.clone(),
                }
                .into());
            } else {
                if account_is_implicit(account_id, config.wasm_config.eth_implicit_accounts) {
                    // If the account doesn't exist and it's implicit, then you
                    // should only be able to create it using single transfer action.
                    // Because you should not be able to add another access key to the account in
                    // the same transaction.
                    // Otherwise you can hijack an account without having the private key for the
                    // public key. We've decided to make it an invalid transaction to have any other
                    // actions on the implicit hex accounts.
                    // The easiest way is to reject the `CreateAccount` action.
                    // See https://github.com/nearprotocol/NEPs/pull/71
                    return Err(ActionErrorKind::OnlyImplicitAccountCreationAllowed {
                        account_id: account_id.clone(),
                    }
                    .into());
                }
            }
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
