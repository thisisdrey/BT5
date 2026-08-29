### Title
Stale `PromiseYieldReceipt` / `PromiseYieldStatus` / `PromiseYieldTimeout` entries survive account deletion and execute against a recreated account with the same id, enabling cross-owner action injection (including `AddKey`) - (`core/store/src/utils/mod.rs`, `runtime/runtime/src/actions.rs`)

### Summary
`remove_account` (`core/store/src/utils/mod.rs:504-575`), which is invoked by `action_delete_account` (`runtime/runtime/src/actions.rs:314-390`), removes `Account`, `ContractCode`, `AccessKey`/`GasKeyNonce`, and `ContractData` trie entries for a deleted account, but never removes `TrieKey::PromiseYieldReceipt`, `TrieKey::PromiseYieldStatus`, the account's entry in the `PromiseYieldTimeout` queue, or `YieldIdToDataId`/`DataIdToYieldId` mappings keyed by that account's id. Because these entries are looked up purely by `(receiver_id, data_id)` with no binding to a particular "incarnation" of the account, they survive account deletion and fire against any future account created under the same id.

### Finding Description
An attacker deploys a contract to its own account `A` and calls `promise_yield_create` targeting itself (`self.ext.create_promise_yield_receipt(self.context.current_account_id.clone())`, `runtime/near-vm-runner/src/wasmtime_runner/logic.rs:4008-4009`). This stores an `ActionReceiptMetadata` with `is_promise_yield = true` and `receiver_id = A`. Before returning the promise, the attacker can attach additional actions to the very same receipt index via the generic promise-batch host functions, e.g. `append_action_add_key_with_full_access` (`runtime/runtime/src/receipt_manager.rs:544-557`), which is not restricted based on whether the receipt `is_promise_yield`.

When the function call completes, the runtime commits this as a `PromiseYield` receipt via `set_promise_yield_receipt` (`core/store/src/utils/mod.rs:200-212`), storing it under `TrieKey::PromiseYieldReceipt{receiver_id: A, data_id}` (`core/primitives/src/trie_key.rs:244-247`), and enqueues a timeout entry via `enqueue_promise_yield_timeout` (`core/store/src/utils/mod.rs:181-198`) and a `PromiseYieldStatus::Yielded` marker.

The attacker then submits `DeleteAccountAction` on `A`. `action_delete_account` (`runtime/runtime/src/actions.rs:314-390`) calls `remove_account` (`core/store/src/utils/mod.rs:504-575`), which only clears `Account`, `ContractCode`, access/gas keys, and `ContractData` — it never touches `PromiseYieldReceipt`, `PromiseYieldStatus`, or the pending `PromiseYieldTimeout` queue entry for `A`. Account `A` is now fully deleted, but the trie still contains the attacker's crafted receipt (FunctionCall + AddKey actions) keyed to receiver `A`.

Later, a third party recreates account `A` (permissionless for top-level ids of length ≥ `min_allowed_top_level_account_length`, e.g. 32/65 chars — see `action_create_account`, `runtime/runtime/src/actions.rs:167-210`, and `core/parameters/src/config.rs:114-131`), deploys a new contract, and funds it.

At the queued timeout height, `resolve_promise_yield_timeouts` (`runtime/runtime/src/lib.rs:3009-3105`) checks `state_update.contains_key(&TrieKey::PromiseYieldReceipt{receiver_id: A, data_id})` — still `true` — and automatically emits a `PromiseResume` receipt with `predecessor_id = receiver_id = A`, no attacker action required. `apply_receipt`'s `PromiseResume` handling (`runtime/runtime/src/lib.rs:1500-1563`) then calls `get_promise_yield_receipt(state_update, account_id, data_id)` and, finding it, executes the stored actions via `apply_action_receipt` against the account currently living at id `A` — i.e., the third party's new account — with `predecessor_id == receiver_id == A` (a "self-call"). Because self-calls are commonly treated as privileged/trusted invocations by contracts (only reachable via the contract's own callback mechanism), and because the attacker could smuggle an `AddKey(FullAccess)` action into the same receipt, this delivers unauthorized `FunctionCall`/`AddKey` execution against an account the attacker never controlled, using authority ("self-call") that the new owner's contract logic did not expect to be forgeable.

No existing check stops this: `remove_account` has no knowledge of pending yields; `resolve_promise_yield_timeouts`/`apply_receipt` verify only key presence, not account continuity/generation; and account-id reuse after deletion is a normal, permissionless part of the protocol.

### Impact Explanation
This is an authorization-escalation bug: a stale, attacker-crafted yield receipt can inject arbitrary `FunctionCall` (with self-call predecessor privilege) and even `AddKey(FullAccess)` actions into whatever account is later created under the same, previously-deleted account id — potentially yielding attacker-controlled full-access keys on a victim's funds-holding account, or bypassing internal "self-call only" authorization checks in the victim's contract. This matches the "authorization escalation across accounts or promises" / theft-of-funds bounty category.

### Likelihood Explanation
Preconditions: attacker must (1) control an account whose id can later be reused (feasible for permissionlessly-creatable top-level names ≥ the length threshold, or via giving up ownership of a sub-account's parent), (2) call `promise_yield_create` targeting itself and optionally batch extra actions (AddKey) onto the same receipt, (3) delete that account, and (4) have some other party recreate the exact same account id before/at the yield timeout (default `yield_timeout_length_in_blocks = 200`) or issue `promise_yield_resume` itself. Steps (1)-(3) are fully within a single attacker's unprivileged control and cheap (only normal gas/storage costs). Step (4) — a third party independently choosing the identical, previously-used account name — is the main constraint on real-world likelihood, though it is entirely plausible for popular/vanity/deterministic naming schemes. The bug is deterministically reproducible in a unit/integration test regardless of that external precondition, since the vulnerable code path (missing cleanup in `remove_account`) is unconditionally reachable.

### Recommendation
When deleting an account (`remove_account` / `action_delete_account`), also purge any state keyed by that account id that can later be "delivered" to a different incarnation of the account: `TrieKey::PromiseYieldReceipt`, `TrieKey::PromiseYieldStatus`, `TrieKey::YieldIdToDataId`/`DataIdToYieldId`, and remove/invalidate the corresponding `PromiseYieldTimeout` queue entries (or tag entries with a generation/nonce tied to account creation and reject delivery when the generation doesn't match). At minimum, `resolve_promise_yield_timeouts` and the `PromiseResume` handling in `apply_receipt` should verify that the receiving account is the same "incarnation" that created the yield (e.g., by binding the yield to an account-creation nonce) before executing the stored receipt.

### Proof of Concept
Integration/runtime-apply test plan:
1. Create account `A` (long top-level name, e.g. 40 chars) with a deployed test contract; fund it.
2. Have `A` call a host function invoking `promise_yield_create` targeting itself, plus (in the same function call) invoke the generic add-key batch action against the returned promise index to append `AddKey(FullAccess, attacker_pubkey)` to the same receipt.
3. Assert `TrieKey::PromiseYieldReceipt{receiver_id: A, data_id}` and a `PromiseYieldTimeout` entry exist in state (as in `get_yield_data_ids_in_state`/`get_yield_data_ids_in_latest_state` helpers used in `test-loop-tests/src/tests/yield_timeouts.rs`).
4. Submit `DeleteAccountAction` for `A` (beneficiary = attacker's other account); apply the receipt.
5. Assert (this is the bug) that `TrieKey::PromiseYieldReceipt{receiver_id: A, data_id}` is **still present** in the resulting state root even though `TrieKey::Account{A}` is gone.
6. Recreate account `A` via `CreateAccountAction` from an unrelated "victim" signer, deploy a different contract, fund it.
7. Advance blocks to the queued timeout height (or send `promise_yield_resume` reproducing `data_id`) and apply the resulting `PromiseResume` receipt.
8. Assert that the victim's new account `A` now has the attacker's `AddKeyAction` public key registered with `FullAccess` permission, proving cross-owner unauthorized action execution. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** runtime/runtime/src/actions.rs (L167-210)
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

    *actor_id = account_id.clone();
    *account = Some(Account::new(
        Balance::ZERO,
        Balance::ZERO,
        AccountContract::None,
        fee_config.storage_usage_config.num_bytes_account,
    ));
}
```

**File:** runtime/runtime/src/actions.rs (L314-390)
```rust
pub(crate) fn action_delete_account(
    state_update: &mut TrieUpdate,
    account: &mut Option<Account>,
    actor_id: &mut AccountId,
    receipt: &Receipt,
    result: &mut ActionResult,
    account_id: &AccountId,
    delete_account: &DeleteAccountAction,
    config: &RuntimeConfig,
    current_protocol_version: ProtocolVersion,
) -> Result<(), StorageError> {
    let account_ref = account.as_ref().unwrap();
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
    let gas_key_balance_to_burn = compute_gas_key_balance_sum(state_update, account_id)?;
    if gas_key_balance_to_burn > GasKeyInfo::MAX_BALANCE_TO_BURN {
        result.result = Err(ActionErrorKind::GasKeyBalanceTooHigh {
            account_id: account_id.clone(),
            public_key: None,
            balance: gas_key_balance_to_burn,
        }
        .into());
        return Ok(());
    }
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
}
```

**File:** runtime/runtime/src/lib.rs (L1495-1563)
```rust
            VersionedReceiptEnum::PromiseYield(_) => {
                // Received a new PromiseYield receipt. We simply store it and await
                // the corresponding PromiseResume receipt.
                set_promise_yield_receipt(state_update, receipt);
            }
            VersionedReceiptEnum::PromiseResume(data_receipt) => {
                if data_receipt.data.is_none() {
                    // This is a timeout resume. Check the status to see if the receipt has been resumed.
                    let status =
                        get_promise_yield_status(state_update, account_id, data_receipt.data_id)?;
                    if status == Some(PromiseYieldStatus::ResumeInitiated) {
                        // A non-timeout resume receipt has been sent, cancel the timeout.
                        return Ok(None);
                    }
                }

                // Received a new PromiseResume receipt delivering input data for a PromiseYield.
                // It is guaranteed that the PromiseYield has exactly one input data dependency
                // and that it arrives first, so we can simply find and execute it.
                if let Some(yield_receipt) =
                    get_promise_yield_receipt(state_update, account_id, data_receipt.data_id)?
                {
                    // Remove the receipt from the state
                    remove_promise_yield_receipt(state_update, account_id, data_receipt.data_id);

                    // Clear the PromiseYield status
                    remove_promise_yield_status(state_update, account_id, data_receipt.data_id);

                    // Clean up yield_id <-> data_id mappings if this was created by yield_create_with_id
                    if ProtocolFeature::YieldWithId.enabled(apply_state.current_protocol_version) {
                        if let Some(yield_id) = get_yield_id_for_data_id(
                            state_update,
                            account_id,
                            data_receipt.data_id,
                        )? {
                            remove_yield_id_mappings(
                                state_update,
                                account_id,
                                yield_id,
                                data_receipt.data_id,
                            );
                        }
                    }

                    // Save the data into the state keyed by the data_id
                    set_received_data(
                        state_update,
                        account_id.clone(),
                        data_receipt.data_id,
                        &ReceivedData { data: data_receipt.data.clone() },
                    );

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
                } else {
```

**File:** runtime/runtime/src/lib.rs (L3009-3098)
```rust
fn resolve_promise_yield_timeouts(
    processing_state: &mut ApplyProcessingReceiptState,
    receipt_sink: &mut ReceiptSink,
    compute_limit: u64,
) -> Result<ResolvePromiseYieldTimeoutsResult, RuntimeError> {
    let mut state_update = &mut processing_state.state_update;
    let total = &mut processing_state.total;
    let apply_state = &processing_state.apply_state;

    let mut promise_yield_indices: PromiseYieldIndices =
        get(state_update, &TrieKey::PromiseYieldIndices)?.unwrap_or_default();
    let initial_promise_yield_indices = promise_yield_indices.clone();
    let mut new_receipt_index: usize = 0;

    let mut processed_yield_timeouts = vec![];
    let yield_processing_start = std::time::Instant::now();
    while promise_yield_indices.first_index < promise_yield_indices.next_available_index {
        if total.compute >= compute_limit || state_update.trie.check_proof_size_limit_exceed() {
            break;
        }

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

            // Record a ReceiptToTx entry for the new resume receipt. The parent is the
            // yield receipt that is being timed out.
            if processing_state.apply_state.save_receipt_to_tx {
                let yield_receipt: Receipt = get_pure(state_update, &promise_yield_key)?
                    .expect("promise yield receipt should exist since contains_key was true");
                processing_state.receipt_to_tx.push((
                    new_receipt_id,
                    ReceiptToTxInfo::V1(ReceiptToTxInfoV1 {
                        origin: ReceiptOrigin::FromReceipt(ReceiptOriginReceipt {
                            parent_receipt_id: *yield_receipt.receipt_id(),
                            parent_predecessor_id: yield_receipt.predecessor_id().clone(),
                        }),
                        receiver_account_id: queue_entry.account_id.clone(),
                        shard_id: processing_state.apply_state.shard_id,
                    }),
                ));
            }

            // The receipt is destined for the local shard and will be placed in the outgoing
            // receipts buffer. It is possible that there is already an outgoing receipt resolving
            // this yield if `yield_resume` was invoked by some receipt which was processed in
            // the current chunk. The ordering will be maintained because the receipts are
            // destined for the same shard; the timeout will be processed second and discarded.
            receipt_sink.forward_or_buffer_receipt(
                resume_receipt,
                apply_state,
                &mut state_update,
            )?;
        }
```

**File:** runtime/runtime/src/receipt_manager.rs (L148-165)
```rust
    pub(super) fn create_promise_yield_receipt(
        &mut self,
        input_data_id: CryptoHash,
        receiver_id: AccountId,
    ) -> ReceiptIndex {
        let new_receipt = ActionReceiptMetadata {
            receiver_id,
            refund_to: None,
            output_data_receivers: vec![],
            input_data_ids: vec![input_data_id],
            actions: vec![],
            is_promise_yield: true,
        };
        let new_receipt_index = self.action_receipts.len();
        self.action_receipts.push(new_receipt);
        self.promise_yield_receipt_index.insert(input_data_id, new_receipt_index);
        new_receipt_index as ReceiptIndex
    }
```

**File:** runtime/runtime/src/receipt_manager.rs (L544-557)
```rust
    pub(super) fn append_action_add_key_with_full_access(
        &mut self,
        receipt_index: ReceiptIndex,
        public_key: PublicKey,
        nonce: Nonce,
    ) {
        self.append_action(
            receipt_index,
            Action::AddKey(Box::new(AddKeyAction {
                public_key,
                access_key: AccessKey { nonce, permission: AccessKeyPermission::FullAccess },
            })),
        );
    }
```
