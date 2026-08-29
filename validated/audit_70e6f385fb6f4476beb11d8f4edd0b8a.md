### Title
Account-lifecycle asymmetry between `action_delete_account`/`remove_account` and pending `PromiseYield` state allows access-key injection into a recreated, name-recycled account - (File: `runtime/runtime/src/actions.rs`, `core/store/src/utils/mod.rs`, `runtime/runtime/src/lib.rs`)

### Summary
`remove_account` (called from `action_delete_account`) deletes `Account`, `ContractCode`, `AccessKey`/gas-key rows, and `ContractData`, but never removes the `TrieKey::PromiseYieldReceipt`, `PromiseYieldStatus`, `PromiseYieldTimeout` queue entry, or `YieldIdToDataId`/`DataIdToYieldId` mappings that a self-targeted `promise_yield_create` call may have left in state. When the account name is later recreated by an unrelated party, `resolve_promise_yield_timeouts` still finds the stale `PromiseYieldReceipt` and synthesizes a `PromiseResume` that re-executes the original yield's action list (which the attacker fully controlled, e.g. `AddKey`) against the *new* account, and `check_actor_permissions` passes trivially because `actor_id` is derived from the stored receipt's own `predecessor_id`, which equals `receiver_id`/`account_id` regardless of the account's actual lifecycle.

### Finding Description
1. An attacker-controlled account `victim.near` (attacker holds full-access key) calls the `promise_yield_create` host function (`runtime/near-vm-runner/src/logic/logic.rs:3660`) with a callback action list containing `Action::AddKey(attacker_pubkey_2)`. This creates a self-targeted `PromiseYield` receipt (`predecessor_id == receiver_id == "victim.near"`), persisted via `set_promise_yield_receipt` under `TrieKey::PromiseYieldReceipt { receiver_id: "victim.near", data_id }` (`runtime/runtime/src/lib.rs:1495-1498`, `core/store/src/utils/mod.rs:200-212`), plus a `PromiseYieldTimeout` queue entry (`enqueue_promise_yield_timeout`, `function_call.rs:162`).
2. In the same or a later block, the attacker submits a self `DeleteAccount` receipt (`predecessor_id == receiver_id == "victim.near"`, `locked == 0`), which passes `check_actor_permissions` (`runtime/runtime/src/actions.rs:761-776`, requires only `actor_id == account_id` and zero `locked`) and reaches `action_delete_account` (`runtime/runtime/src/actions.rs:314`), which calls `remove_account` (`core/store/src/utils/mod.rs:504-575`). `remove_account` only removes `Account`, `ContractCode`, `AccessKey`/gas-key rows, and `ContractData` — it never touches `PromiseYieldReceipt`, `PromiseYieldStatus`, `PromiseYieldTimeout`, or yield-id mappings.
3. A third party (e.g. `near` registrar, or anyone entitled to the freed name) later issues `CreateAccount` for `"victim.near"` again, establishing a brand-new, unrelated account under the same `account_id`.
4. When the timeout height is reached, `resolve_promise_yield_timeouts` (`runtime/runtime/src/lib.rs:3009`) walks the `PromiseYieldTimeout` queue, and for the still-present entry checks `state_update.contains_key(&promise_yield_key, ...)` (`lib.rs:3051`) — this is still `true` because `remove_account` never cleared it. It then synthesizes a `PromiseResume` receipt with `receiver_id == predecessor_id == "victim.near"` and forwards it (`lib.rs:3060-3097`).
5. That `PromiseResume` is processed by `process_receipt` (`lib.rs:1500-1568`), which loads the stale yield receipt via `get_promise_yield_receipt`, removes the PromiseYield bookkeeping, and calls `apply_action_receipt` on the original (attacker-authored) receipt.
6. Inside `apply_action_receipt`, `actor_id` is initialized as `receipt.predecessor_id().clone()` (`lib.rs:855`) — a value baked into the receipt at creation time, which for this self-yield equals `account_id` ("victim.near"). `check_actor_permissions` for `Action::AddKey` only checks `actor_id != account_id` (`actions.rs:753-759`); since both are literally the same string, the check passes unconditionally, and `action_add_key` installs `attacker_pubkey_2` as a key on the brand-new account, with no relation to the new account's real owner.

The root cause is an authorization-exactness violation: `check_actor_permissions` verifies string equality between `actor_id` and `account_id`, not that the entity granting the promise still legitimately owns the account in its current lifetime. Combined with `remove_account`'s incomplete cleanup of yield-related trie keys, a deleted-and-recreated account inherits privileged actions from its prior incarnation.

### Impact Explanation
This is a full authorization-escalation / access-key takeover: an attacker can inject an arbitrary access key (including a full-access key) into any account that reuses a name the attacker previously owned and deleted, without any privilege in the new account's lifetime. This falls under "authorization escalation across accounts or promises" and enables theft of funds/control of the new account. It requires no validator, node, or leaked-key access — only ordinary transaction submission from an unprivileged attacker account.

### Likelihood Explanation
Preconditions are attacker-controlled and cheap: the attacker only needs to own an account it is willing to delete (any sub-account they control, or a top-level name they may attempt to make attractive for reuse/registration by someone else), issue a `promise_yield_create` call with an `AddKey` callback, then self-delete the account before or shortly after the yield resolves. The exploit is fully repeatable and depends only on a third party later recreating the exact same `account_id` — a realistic scenario for name-squatting/name-recycling patterns (e.g., short/desirable sub-account names, or accounts users expect to reuse). No race against block producers or timing precision beyond the configured `yield_timeout_length_in_blocks` is required.

### Recommendation
`remove_account` (or `action_delete_account`) must also purge all outstanding `PromiseYield`-related state for the account being deleted: iterate and remove any `TrieKey::PromiseYieldReceipt`, `TrieKey::PromiseYieldStatus`, `TrieKey::YieldIdToDataId`/`DataIdToYieldId` mappings, and dequeue/invalidate corresponding `TrieKey::PromiseYieldTimeout` entries for that account before the account can be deleted (or reject `DeleteAccount` while any such pending yield exists). Additionally, `resolve_promise_yield_timeouts`/`process_receipt` should verify the account still exists and matches the state at yield-creation time (e.g., via an account-generation/nonce check) before resuming and executing the stored action list, ensuring authorization exactness is enforced independent of trie-key cleanup completeness.

### Proof of Concept
Integration test plan (apply-path/test-loop, similar to `test-loop-tests/src/tests/yield_timeouts.rs` and `create_delete_account.rs`):
1. Deploy a contract to `victim.near` (attacker-owned) that calls `promise_yield_create` with a callback batch containing `Action::AddKey(attacker_pubkey_2)` targeting itself.
2. In the same or next block, submit a self `DeleteAccount` transaction for `victim.near` (zero `locked`), confirming it succeeds and the `Account`/`AccessKey` rows are gone from state.
3. Submit a `CreateAccount` transaction from a legitimate registrar/predecessor recreating `"victim.near"` with a fresh owner key `owner_pubkey_new`.
4. Advance the chain to `yield_timeout_height` and one block beyond, verifying a `PromiseResume` receipt is produced and applied for `victim.near` (mirroring `find_promise_resume_receipt_ids_from_latest_block` and `run_until_executed_height` helpers).
5. Assert that the new account's access-key set (queried via RPC/view) now contains `attacker_pubkey_2`, proving unauthorized key injection into the recreated account — expected (fixed) behavior is that no such key appears and/or the resume receipt fails/no-ops because the pending yield state was purged at deletion time. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** runtime/runtime/src/lib.rs (L853-856)
```rust
        let mut account = get_account(state_update, account_id)?;
        let account_did_not_exist = account.is_none();
        let mut actor_id = receipt.predecessor_id().clone();
        let mut result = ActionReceiptResult::new();
```

**File:** runtime/runtime/src/lib.rs (L1495-1568)
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
                    // If the user happens to call `promise_yield_resume` multiple times, it may so
                    // happen that multiple PromiseResume receipts are delivered. We can safely
                    // ignore all but the first.
                    return Ok(None);
                }
```

**File:** runtime/runtime/src/lib.rs (L3009-3104)
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

        processed_yield_timeouts.push(queue_entry);
        state_update.remove(queue_entry_key);
        // Math checked above: first_index is less than next_available_index
        promise_yield_indices.first_index += 1;
    }
```

**File:** runtime/near-vm-runner/src/logic/logic.rs (L3660-3718)
```rust
    pub fn promise_yield_create(
        &mut self,
        method_name_len: u64,
        method_name_ptr: u64,
        arguments_len: u64,
        arguments_ptr: u64,
        gas: u64,
        gas_weight: u64,
        register_id: u64,
    ) -> Result<u64> {
        self.result_state.gas_counter.pay_base(base)?;
        if self.context.is_view() {
            return Err(HostError::ProhibitedInView {
                method_name: "promise_yield_create".to_string(),
            }
            .into());
        }
        self.result_state.gas_counter.pay_base(yield_create_base)?;

        let method_name = get_memory_or_register!(self, method_name_ptr, method_name_len)?;
        if method_name.is_empty() {
            return Err(HostError::EmptyMethodName.into());
        }
        let arguments = get_memory_or_register!(self, arguments_ptr, arguments_len)?;
        let method_name = method_name.into_owned();
        let arguments = arguments.into_owned();

        // Input can't be large enough to overflow, WebAssembly address space is 32-bits.
        let num_bytes = method_name.len() as u64 + arguments.len() as u64;
        self.result_state.gas_counter.pay_per(yield_create_byte, num_bytes)?;
        // Prepay gas for the callback so that it cannot be used for this execution any longer.
        self.result_state.gas_counter.prepay_gas(Gas::from_gas(gas))?;

        // Here we are creating a receipt with a single data dependency which will then be
        // resolved by the resume call.
        self.pay_gas_for_new_receipt(true, &[true])?;
        let (new_receipt_idx, data_id) =
            self.ext.create_promise_yield_receipt(self.context.current_account_id.clone())?;

        let new_promise_idx = self.checked_push_promise(Promise::Receipt(new_receipt_idx))?;
        self.pay_action_base(ActionCosts::function_call_base, true)?;
        self.pay_action_per_byte(ActionCosts::function_call_byte, num_bytes, true)?;
        self.ext.append_action_function_call_weight(
            new_receipt_idx,
            method_name,
            arguments,
            Balance::ZERO,
            Gas::from_gas(gas),
            GasWeight(gas_weight),
        )?;

        self.registers.set(
            &mut self.result_state.gas_counter,
            &self.config.limit_config,
            register_id,
            *data_id.as_bytes(),
        )?;
        Ok(new_promise_idx)
    }
```
