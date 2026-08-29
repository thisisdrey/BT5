Confirmed: `actor_id` for the postponed receipt's action execution is initialized as `receipt.predecessor_id().clone()` [1](#0-0) , i.e. exactly the predecessor recorded when the receipt was originally created (the self-account name). Since `check_actor_permissions` for `Action::DeleteAccount` only compares `actor_id != account_id` (both AccountId strings) and checks `account.locked().is_zero()` on the account loaded fresh from state at delivery time [2](#0-1) , there is no notion of account "incarnation"/generation tying the stale postponed receipt to the specific account instance that created it.

### Title
Stale postponed DeleteAccount receipt executes against a recreated account, diverting a new depositor's funds to an attacker-chosen beneficiary - (File: runtime/runtime/src/lib.rs, runtime/runtime/src/actions.rs, core/store/src/utils/mod.rs)

### Summary
A self-authorized `DeleteAccount` action postponed on a missing `input_data_id` is stored under `TrieKey::PostponedReceipt{receiver_id, receipt_id}` / `PostponedReceiptId{receiver_id, data_id}` keyed purely by account name [3](#0-2) . `remove_account` (invoked by an unrelated, immediate `DeleteAccount` on the same account) clears `Account`, `ContractCode`, access keys and contract data, but never clears `PostponedReceipt`/`PostponedReceiptId`/`PendingDataCount` [4](#0-3) . If the account is later recreated under the same name and funded by an unrelated depositor, delivering the originally-awaited `DataReceipt` resurrects and executes the stale postponed receipt against the new account, passing `check_actor_permissions` purely by name equality and re-running `action_delete_account`, which pays out the new account's current balance to the beneficiary chosen by the long-deleted incarnation.

### Finding Description
`process_action_receipt` postpones an action receipt whose `input_data_ids` aren't yet satisfied, storing it and a pending-data counter keyed only by `(receiver_id, receipt_id)`/`(receiver_id, data_id)` [5](#0-4) . When a matching `Data` receipt later arrives, `process_receipt`'s Data arm looks up `PostponedReceiptId` for `(receiver_id, data_id)`, decrements `PendingDataCount`, and once it hits zero, fetches and executes the stored receipt via `apply_action_receipt` — with no check that the account is the same "incarnation" that queued it [6](#0-5) .

`remove_account` (called from `action_delete_account`) only clears `Account`, `ContractCode`, access/gas keys, and contract data [7](#0-6) ; it does not remove any `PostponedReceipt`, `PostponedReceiptId`, or `PendingDataCount` entries. Consequently, if the account is deleted by a separate, immediate receipt (e.g., a single-action `DeleteAccount` receipt, which is an instant receipt per `Receipt::is_instant_receipt`) while a different, data-dependent postponed `DeleteAccount(beneficiary=attacker)` receipt is still awaiting its `data_id`, that stale postponed receipt survives account deletion in the trie.

Anyone can then recreate the account (`CreateAccount` + `Transfer`), whose validation (`check_account_existence`) only checks whether the account currently exists, not whether stale postponed receipts reference it [8](#0-7) . When the attacker (who controls the promise dependency the original account was awaiting) finally delivers the `DataReceipt` for that `data_id`, `apply_action_receipt` loads the *new* account, sets `actor_id = receipt.predecessor_id()` from the stored (old) receipt [1](#0-0) , and `check_actor_permissions` for `DeleteAccount` only requires `actor_id == account_id` (both are just the same AccountId string, self-call) and `account.locked().is_zero()` [2](#0-1)  — both trivially satisfied by the freshly-created account. `action_delete_account` then pays out `account_ref.amount()` (the new depositor's balance) to the beneficiary recorded in the stale receipt (the attacker) and calls `remove_account` again [9](#0-8) .

No existing check (signature, nonce, access-key, `check_account_existence`, `check_actor_permissions`, storage staking, or receipt size/validation) ties a postponed receipt to a specific account "generation"; the runtime's account model treats accounts purely by name, and the postponed-receipt storage keys are similarly name-based with no cleanup on deletion.

### Impact Explanation
This is a direct theft-of-funds primitive: an unrelated, unwitting depositor's newly-deposited balance is redirected to an attacker-chosen `beneficiary_id` via an authorization token (`predecessor_id == account_id`) issued by an account incarnation that no longer exists at execution time. This matches the "theft of funds" / "authorization escalation across accounts or promises" bounty categories, since the check_actor_permissions authorization is not scoped to the account instance that created it.

### Likelihood Explanation
Exploitation requires: (1) a contract account that creates a self-`DeleteAccount(beneficiary=attacker)` receipt with an outstanding `input_data_id` (e.g., via `promise_and`/`promise_then` chained off a promise resolved by an attacker-controlled callee), (2) that same account being deleted via a separate, immediate path before the data arrives, and (3) an unrelated third party recreating the account and depositing funds before the attacker chooses to deliver the pending data. Preconditions (1) and (2) generally require the *original* account owner to construct this sequence themselves (whether intentionally malicious, buggy, or a griefing setup), and (3) requires an independent, unaware depositor to reuse that exact account name. This is a low-cost, fully attacker-controlled-timing exploit once the preconditions are met (attacker just calls their own contract to emit the missing data whenever a fresh deposit lands), and it is repeatable for any account name where this pattern recurs. This is a niche but concrete and reachable configuration purely from ordinary transactions with no privileged access needed.

### Recommendation
Tie postponed action receipts (and their `PendingDataCount`/`PostponedReceiptId` entries) to the account's lifetime: either (a) have `remove_account` iterate and purge all `PostponedReceipt`/`PostponedReceiptId`/`PendingDataCount`/`ReceivedData` entries for the deleted `account_id`, or (b) introduce an account "incarnation" nonce/generation counter that is captured when a receipt is postponed and re-validated against the current account's generation before executing a resurrected postponed receipt, failing/dropping the receipt if the generation no longer matches.

### Proof of Concept
Integration/runtime-apply test plan:
1. Set up account `child.near` with a contract; have it issue two receipts targeting itself in the same or adjacent chunks:
   - Receipt A: `ActionReceipt{predecessor_id: child.near, receiver_id: child.near, input_data_ids: [data_id], actions: [DeleteAccount{beneficiary_id: attacker.near}]}` — do not deliver `data_id` yet, so it gets postponed (assert `PostponedReceipt`/`PostponedReceiptId`/`PendingDataCount` are set in state).
   - Receipt B: `ActionReceipt{predecessor_id: child.near, receiver_id: child.near, input_data_ids: [], actions: [DeleteAccount{beneficiary_id: someone_else.near}]}` — executes instantly, deleting `child.near`.
2. Apply a `CreateAccount` + `Transfer(deposit=X)` receipt from `victim2.near` targeting `child.near`, recreating it with balance X. Assert the account exists with `amount() == X`.
3. Deliver a `Data` receipt with the previously-referenced `data_id` targeting `child.near`.
4. Assert: the stale postponed receipt executes (`ExecutionOutcome` shows `DeleteAccount` success on `child.near`), `child.near` no longer exists afterward, and a balance-refund receipt of amount X is generated to `attacker.near` — with no transaction signed by `victim2.near` authorizing this transfer.
5. Additionally assert that `PostponedReceipt`/`PostponedReceiptId`/`PendingDataCount` keys for the pre-recreation `receipt_id` were still present in state immediately after step 1's `remove_account` call, confirming `remove_account` does not clean them up.

### Citations

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

**File:** runtime/runtime/src/actions.rs (L364-371)
```rust
    // We use current amount as a pay out to beneficiary.
    let account_balance = account_ref.amount();
    if account_balance > Balance::ZERO {
        result
            .new_receipts
            .push(Receipt::new_balance_refund(&delete_account.beneficiary_id, account_balance));
    }
    let remove_result = remove_account(state_update, account_id)?;
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

**File:** runtime/runtime/src/actions.rs (L794-818)
```rust
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
