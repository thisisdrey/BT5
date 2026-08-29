### Title
Stale `PromiseYieldReceipt`/`PromiseYieldStatus`/`YieldIdToDataId`/`DataIdToYieldId` rows survive `remove_account`, letting a deleted account's yield callback execute as a "self-call" against a later, differently-owned account with the same name - (File: core/store/src/utils/mod.rs)

### Summary
`remove_account` deletes `Account`, `ContractCode`, access/gas keys and `ContractData`, but never touches `TrieKey::PromiseYieldReceipt`, `TrieKey::PromiseYieldStatus`, `TrieKey::YieldIdToDataId`, or `TrieKey::DataIdToYieldId` rows keyed by that account as `receiver_id`. [1](#0-0)  When an account calls `promise_yield_create`, a `PromiseYieldReceipt` is stored keyed by `receiver_id = current_account_id` (a self-targeted receipt) together with a `PromiseYieldTimeout` queue entry that fires automatically after a fixed number of blocks regardless of any cooperation from a "resolver". [2](#0-1) [3](#0-2)  If that account is deleted before the timeout fires, and the account name is later reused by a different owner (e.g., a long top-level account name, or a subaccount recreated by a shared parent/factory), the leftover receipt still exists at that key and the timeout resolver `resolve_promise_yield_timeouts` will unconditionally deliver a `PromiseResume` to the account currently occupying that id. [4](#0-3) 

### Finding Description
1. Attacker deploys a contract to account `A` and calls `promise_yield_create(method_name, arguments, gas, gas_weight)`. This appends a `FunctionCall` action (attacker-chosen `method_name`/`arguments`) to a new receipt whose `receiver_id == A` and stores it under `TrieKey::PromiseYieldReceipt { receiver_id: A, data_id }` once delivered, plus a `PromiseYieldTimeout { index }` entry in the global timeout queue with a fixed `expires_at` block height. [2](#0-1) [5](#0-4) 
2. Attacker submits `DeleteAccount(A)` before the yield times out. `action_delete_account` calls `remove_account`, which only removes `Account`, `ContractCode`, access/gas keys and `ContractData` — the `PromiseYieldReceipt`/`PromiseYieldStatus`/`YieldIdToDataId`/`DataIdToYieldId` rows for `A` are never enumerated or removed. [6](#0-5) [1](#0-0) 
3. Account name `A` is later recreated (by the attacker themselves for a long top-level name, by a shared parent account for a subaccount, or by a factory-style contract pattern that redeploys a fresh contract under the same subaccount name for a new, unrelated user). `check_account_existence`/`action_create_account` place a brand-new `Account` at the same `AccountId`, with no notion that stale cross-referencing state exists elsewhere in the trie. [7](#0-6) 
4. When `promise_yield_indices` reach the stale entry's `expires_at`, `resolve_promise_yield_timeouts` checks only `state_update.contains_key(&promise_yield_key)` for `TrieKey::PromiseYieldReceipt{receiver_id: A, data_id}` — this check succeeds because the row was never cleaned up — and unconditionally emits a `PromiseResume` receipt destined for `A`. [4](#0-3) 
5. `apply_action_receipt` for the resulting `PromiseResume` fetches the account fresh via `get_account(state_update, account_id)` — this now returns the **new** occupant's account — and executes the attacker's stored `FunctionCall` action against it. [8](#0-7) [9](#0-8)  Because the receipt's `predecessor_id`/`receiver_id` are both `A` (a self-directed receipt), the call executes with `predecessor_id() == current_account_id()`, which is the exact condition many NEAR contracts use to gate privileged/internal-only methods (`assert_self()`/owner-callback patterns). This lets the original attacker's pre-planted method call bypass the new occupant's intended access control, since the runtime has no way to distinguish "the same account before/after deletion" from "the same predecessor identity."
6. `check_account_existence`/`AccountDoesNotExist` checks only protect against calls to accounts that don't exist at all — they do nothing to prevent this because at execution time the recreated account *does* exist. [10](#0-9)  No code path re-validates that the `PromiseYieldReceipt`'s `receiver_id`'s account is the *same* account instance that created the yield.

### Impact Explanation
This is an authorization-escalation primitive: an attacker can plant an arbitrary self-authorized `FunctionCall` (chosen `method_name`/`arguments`, funded with attacker-prepaid gas at plant time) that fires automatically (no cooperation needed — the timeout path alone suffices) against whatever account occupies the same `AccountId` after the original account is deleted and the name is reused. Against a contract using the common "self-call" trust pattern for privileged operations (owner/admin methods, migration hooks, callback-only mutators), this can escalate to unauthorized calls being made as if the *new* owner's contract called itself, satisfying `predecessor_id == current_account_id`. Matching NEAR bounty category: "authorization escalation across accounts or promises."

### Likelihood Explanation
Preconditions: attacker needs to control account `A` (deploy contract, call `promise_yield_create`, then self-`DeleteAccount`), and the account name must be reused within the yield's timeout window (`yield_timeout_length_in_blocks`, protocol-configured). Cost to the attacker is limited to one contract deployment and normal transaction/gas fees; the attack is fully deterministic and repeatable for subaccount-reuse patterns (e.g., factory/pool/DAO templates that delete-and-recreate subaccounts under a shared parent) where the attacker can either be first to occupy a to-be-reused name, or control the parent account that recreates it. For arbitrary unrelated top-level accounts the collision requires a name to be reused by coincidence or through a predictable subaccount-recycling workflow, which narrows — but does not eliminate — real-world feasibility; the strongest, clearly reachable case is attacker-controlled subaccount name recycling under a shared parent/factory pattern.

### Recommendation
Update `remove_account` (core/store/src/utils/mod.rs) to also enumerate and remove all `TrieKey::PromiseYieldReceipt`, `TrieKey::PromiseYieldStatus`, `TrieKey::YieldIdToDataId`, and `TrieKey::DataIdToYieldId` rows for the account being deleted (mirroring how access keys and contract data are enumerated via prefix and removed), and correspondingly drop/skip any queued `PromiseYieldTimeout` entries whose `account_id` was deleted (e.g., check account existence in `resolve_promise_yield_timeouts` in addition to `contains_key` on the yield receipt, or invalidate on deletion). Alternatively, disallow `DeleteAccount` while pending yielded promises exist for that account.

### Proof of Concept
Integration test plan (extending `runtime/runtime/src/tests/apply.rs` patterns already used for yield/timeout and delete/recreate, e.g. `test_promise_input_size_limit_exceeded_fails_and_cleans_up` and `test_function_call_after_same_chunk_delete_recreate_resolves_fresh_code`):
1. Deploy a yield/resume-capable contract to subaccount `child.alice.near`; call `promise_yield_create` to register a callback with attacker-chosen `method_name`/`arguments` and record `data_id`.
2. Assert `TrieKey::PromiseYieldReceipt{receiver_id: child, data_id}` and the corresponding `PromiseYieldTimeout` queue entry exist in state.
3. Submit `DeleteAccount(child.alice.near)` (beneficiary `alice.near`) in a later chunk; apply and commit.
4. Assert (bug reproduction) that `get_promise_yield_receipt`/`TrieKey::PromiseYieldStatus`/`YieldIdToDataId` rows for `child.alice.near` **still exist** post-deletion (they should not).
5. Recreate `child.alice.near` via `CreateAccount` + `DeployContract` with a *different* contract that gates a privileged method behind `predecessor_id() == env::current_account_id()`.
6. Advance `block_height` past the original `expires_at` so `resolve_promise_yield_timeouts` fires; apply the chunk.
7. Assert the privileged method executed on the *new* contract's account state (state mutation observed), demonstrating that the stale yield callback bypassed the new contract's self-call gate — proving cross-ownership authorization escalation via account-name reuse.

### Citations

**File:** core/store/src/utils/mod.rs (L181-212)
```rust
// Enqueues given timeout to the PromiseYield timeout queue
pub fn enqueue_promise_yield_timeout(
    state_update: &mut TrieUpdate,
    promise_yield_indices: &mut PromiseYieldIndices,
    account_id: AccountId,
    data_id: CryptoHash,
    expires_at: BlockHeight,
) {
    set(
        state_update,
        TrieKey::PromiseYieldTimeout { index: promise_yield_indices.next_available_index },
        &PromiseYieldTimeout { account_id, data_id, expires_at },
    );
    promise_yield_indices.next_available_index = promise_yield_indices
        .next_available_index
        .checked_add(1)
        .expect("Next available index for PromiseYield timeout queue exceeded the integer limit");
}

pub fn set_promise_yield_receipt(state_update: &mut TrieUpdate, receipt: &Receipt) {
    match receipt.versioned_receipt() {
        VersionedReceiptEnum::PromiseYield(action_receipt) => {
            assert!(action_receipt.input_data_ids().len() == 1);
            let key = TrieKey::PromiseYieldReceipt {
                receiver_id: receipt.receiver_id().clone(),
                data_id: action_receipt.input_data_ids()[0],
            };
            set(state_update, key, receipt);
        }
        _ => unreachable!("Expected PromiseYield receipt"),
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

**File:** runtime/near-vm-runner/src/logic/logic.rs (L3693-3717)
```rust
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
```

**File:** runtime/runtime/src/lib.rs (L853-856)
```rust
        let mut account = get_account(state_update, account_id)?;
        let account_did_not_exist = account.is_none();
        let mut actor_id = receipt.predecessor_id().clone();
        let mut result = ActionReceiptResult::new();
```

**File:** runtime/runtime/src/lib.rs (L1495-1499)
```rust
            VersionedReceiptEnum::PromiseYield(_) => {
                // Received a new PromiseYield receipt. We simply store it and await
                // the corresponding PromiseResume receipt.
                set_promise_yield_receipt(state_update, receipt);
            }
```

**File:** runtime/runtime/src/lib.rs (L1500-1562)
```rust
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
```

**File:** runtime/runtime/src/lib.rs (L3009-3068)
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

**File:** runtime/runtime/src/actions.rs (L787-855)
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
        Action::Transfer(_) => {
            if account.is_none() {
                return check_transfer_to_nonexisting_account(
                    config,
                    account_id,
                    implicit_account_creation_eligible,
                );
            }
        }
        Action::DeterministicStateInit(_) => {
            // Existing and non existing is valid for DeterministicStateInit.
            // Does not exist => The account will be created by the action.
            // Does exist => Nothing happens but the receipt is not aborted to
            // allow optional init before other actions.
        }
        Action::DeployContract(_)
        | Action::FunctionCall(_)
        | Action::Stake(_)
        | Action::AddKey(_)
        | Action::DeleteKey(_)
        | Action::DeleteAccount(_)
        | Action::Delegate(_)
        | Action::DelegateV2(_)
        | Action::DeployGlobalContract(_)
        | Action::UseGlobalContract(_)
        | Action::TransferToGasKey(_)
        | Action::WithdrawFromGasKey(_) => {
            if account.is_none() {
                return Err(ActionErrorKind::AccountDoesNotExist {
                    account_id: account_id.clone(),
                }
                .into());
            }
        }
    };
    Ok(())
}
```
