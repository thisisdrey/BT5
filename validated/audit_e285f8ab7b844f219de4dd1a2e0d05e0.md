### Title
Stale postponed AddKey receipts survive `remove_account` and execute against a re-created account with the same name - ([File: core/store/src/utils/mod.rs])

### Summary
`remove_account` deletes an account's `Account`, `ContractCode`, `AccessKey`/gas-key rows and `ContractData`, but never purges the account's receipt-queue bookkeeping (`ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, and the PromiseYield equivalents) that is keyed only by `account_id`/`receiver_id`. A self-authorized action receipt (e.g. an `AddKey{full_access}` batch created by account `A` calling itself, gated on an unresolved `input_data_id`) that was postponed before `A` was deleted remains parked in state and will be resurrected and executed the moment a matching `DataReceipt` arrives - even if `A` has since been deleted and re-created by an unrelated party.

### Finding Description
`process_action_receipt` (`runtime/runtime/src/lib.rs:1593-1658`) never checks whether the receiver account exists before postponing a receipt: it only records `PostponedReceiptId`, `PendingDataCount`, and the receipt itself (`set_postponed_receipt`) keyed by `receiver_id`. [1](#0-0) 

When a matching `DataReceipt` later arrives, `process_receipt`'s data branch looks the postponed receipt up purely by `(receiver_id, data_id)` and, once the pending count reaches zero, unconditionally calls `apply_action_receipt` on the stored action receipt. [2](#0-1) 

At execution time, `apply_action` runs `check_account_existence` (requires `account.is_some()` for `AddKey`, satisfied trivially if *any* account with that name now exists) and `check_actor_permissions`. [3](#0-2)  For a self-call receipt, `actor_id` is initialized to `receipt.predecessor_id()` [4](#0-3)  which equals `account_id` for a self-targeted receipt — so the permission check passes without any re-validation that the account currently occupying that name is still controlled by the original signer.

The root cause is that `remove_account` only clears `Account`, `ContractCode`, access/gas keys, and `ContractData`; it leaves `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `PromiseYieldReceipt`/`PromiseYieldStatus`, and `YieldIdToDataId`/`DataIdToYieldId` rows (all listed in `col::COLUMNS_WITH_ACCOUNT_ID_IN_KEY`) untouched for the deleted `account_id`. [5](#0-4) [6](#0-5) 

Exploit flow:
1. Attacker creates account `A` (must be permissionlessly creatable, e.g. a top-level name at or above `min_allowed_top_level_account_length`, per `action_create_account`'s top-level branch which allows any predecessor once the length threshold is met). [7](#0-6) 
2. Attacker sends a `FunctionCall` from `A` to itself creating a promise batch with `AddKey{full_access, attacker_pubkey}` whose completion is gated on an `input_data_id` the attacker controls (e.g. a pending cross-contract callback). This gets stored as a postponed receipt under `receiver_id = A`.
3. Attacker deletes `A` via `DeleteAccount` (`action_delete_account` → `remove_account`), which refunds the balance but leaves the postponed-receipt bookkeeping in place. [8](#0-7) 
4. A victim (unaware the name was previously used) creates an account named `A` and funds it via `CreateAccount` + `Transfer`.
5. Attacker's second, independent receipt delivers the outstanding `DataReceipt`, satisfying the stale dependency; the runtime finds `account.is_some()` (the victim's fresh account) and `actor_id == account_id`, so `check_account_existence`/`check_actor_permissions` both pass, and the `AddKey` executes, granting the attacker a full-access key on the victim's funded account.
6. Attacker signs a `Transfer` with the smuggled key and drains the account.

### Impact Explanation
Concrete theft of user funds via an unauthorized full-access key silently granted on a victim's freshly funded account — no signature from the victim was ever obtained for the key grant, violating authorization exactness. This maps to NEAR's "theft of user funds" / "authorization escalation across accounts" bounty category.

### Likelihood Explanation
The attacker needs full control over the pre-deletion setup (account creation, the postponed batch, and timing of the completing `DataReceipt`), all of which are actions available to any ordinary funded account with no special privilege. The main constraint is getting a victim to independently create and fund an account with the exact same `account_id` the attacker previously used and deleted; this is realistic only where the account name is attacker-supplied/predictable to the victim (e.g. a long permissionless top-level name presented as a deposit/vanity address, or reused after a normal delete/recreate cycle by the same integrator). Given that requirement, the underlying protocol-level gap (stale cross-lifetime receipt state, no re-authorization at execution) is fully attacker-repeatable and costs only standard gas/storage fees.

### Recommendation
`remove_account` should purge all `account_id`/`receiver_id`-keyed rows before/along with account removal, including `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, and `DataIdToYieldId` for that account. Alternatively/additionally, `apply_action_receipt`/`process_receipt` should bind a postponed receipt to the account's identity/nonce at postponement time (e.g. an account "generation" counter or the state root's account creation height) and refuse to execute it if the receiver account was deleted and re-created in the interim.

### Proof of Concept
Runtime integration test (in `runtime/runtime/src/tests/apply.rs` style, mirroring `test_function_call_after_same_chunk_delete_recreate_resolves_fresh_code`):
1. Create account `A`, deploy a helper contract, and drive a `FunctionCall` receipt that creates a promise batch `AddKey{full_access, attacker_pubkey}` with an `input_data_id` that is never resolved in this apply call — assert it lands as `PostponedReceipt` in state.
2. Apply a `DeleteAccount` receipt for `A` (beneficiary = attacker) in a later block; assert `get_account(A)` is `None` but `get_postponed_receipt`/`PendingDataCount`/`PostponedReceiptId` for `A` still return `Some(..)`.
3. Apply `CreateAccount` + `Transfer(deposit)` receipts for `A` from a different, "victim" predecessor; record `victim_amount_before = Account(A).amount()`.
4. Apply the outstanding `DataReceipt` that completes the dependency; assert the postponed `AddKey` executes successfully (no `AccountDoesNotExist`/`ActorNoPermission` error) and `A` now has the attacker's full-access key.
5. Apply a `Transfer` signed with the attacker's key draining `A`; assert `Account(A).amount()` dropped from `victim_amount_before` to (near) zero, proving unauthorized fund theft — the invariant `amount unchanged unless victim authorized the key` is violated.

### Citations

**File:** runtime/runtime/src/lib.rs (L552-567)
```rust
        // Account validation
        if let Err(e) = check_account_existence(
            action,
            account,
            account_id,
            &apply_state.config,
            implicit_account_creation_eligible,
        ) {
            result.result = Err(e);
            return Ok(result);
        }
        // Permission validation
        if let Err(e) = check_actor_permissions(action, account, actor_id, account_id) {
            result.result = Err(e);
            return Ok(result);
        }
```

**File:** runtime/runtime/src/lib.rs (L853-856)
```rust
        let mut account = get_account(state_update, account_id)?;
        let account_did_not_exist = account.is_none();
        let mut actor_id = receipt.predecessor_id().clone();
        let mut result = ActionReceiptResult::new();
```

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

**File:** core/primitives/src/trie_key.rs (L87-102)
```rust
    /// All columns except those used for the delayed receipts queue, the yielded promises
    /// queue, and the outgoing receipts buffer, which are global state for the shard.
    pub const COLUMNS_WITH_ACCOUNT_ID_IN_KEY: [(u8, &str); 12] = [
        (ACCOUNT, "Account"),
        (CONTRACT_CODE, "ContractCode"),
        (ACCESS_KEY, "AccessKey"),
        (RECEIVED_DATA, "ReceivedData"),
        (POSTPONED_RECEIPT_ID, "PostponedReceiptId"),
        (PENDING_DATA_COUNT, "PendingDataCount"),
        (POSTPONED_RECEIPT, "PostponedReceipt"),
        (CONTRACT_DATA, "ContractData"),
        (PROMISE_YIELD_RECEIPT, "PromiseYieldReceipt"),
        (PROMISE_YIELD_STATUS, "PromiseYieldStatus"),
        (YIELD_ID_TO_DATA_ID, "YieldIdToDataId"),
        (DATA_ID_TO_YIELD_ID, "DataIdToYieldId"),
    ];
```

**File:** runtime/runtime/src/actions.rs (L167-201)
```rust
pub(crate) fn action_create_account(
    fee_config: &RuntimeFeesConfig,
    account_creation_config: &AccountCreationConfig,
    account: &mut Option<Account>,
    actor_id: &mut AccountId,
    account_id: &AccountId,
    predecessor_id: &AccountId,
    result: &mut ActionResult,
) {
    if account_id.is_top_level() {
        if account_id.len() < account_creation_config.min_allowed_top_level_account_length as usize
            && predecessor_id != &account_creation_config.registrar_account_id
        {
            // A short top-level account ID can only be created registrar account.
            result.result = Err(ActionErrorKind::CreateAccountOnlyByRegistrar {
                account_id: account_id.clone(),
                registrar_account_id: account_creation_config.registrar_account_id.clone(),
                predecessor_id: predecessor_id.clone(),
            }
            .into());
            return;
        } else {
            // OK: Valid top-level Account ID
        }
    } else if !account_id.is_sub_account_of(predecessor_id) {
        // The sub-account can only be created by its root account. E.g. `alice.near` only by `near`
        result.result = Err(ActionErrorKind::CreateAccountNotAllowed {
            account_id: account_id.clone(),
            predecessor_id: predecessor_id.clone(),
        }
        .into());
        return;
    } else {
        // OK: Valid sub-account ID by proper predecessor.
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
