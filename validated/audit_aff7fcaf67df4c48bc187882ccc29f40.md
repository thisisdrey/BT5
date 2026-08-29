Confirmed vulnerable path.

### Title
Postponed action receipts survive DeleteAccount and execute against a recreated account, enabling name-scoped authorization escalation via stale AddKey - ([File: runtime/runtime/src/lib.rs / core/store/src/utils/mod.rs])

### Summary
`process_action_receipt` (`runtime/runtime/src/lib.rs:1593-1658`) postpones an `ActionReceipt` (including its raw `Action::AddKey` payload) keyed only by `(receiver_id, receipt_id)` whenever its `input_data_ids` are not yet satisfied. `remove_account` (`core/store/src/utils/mod.rs:504-575`), invoked by `action_delete_account` (`runtime/runtime/src/actions.rs:314-390`), deletes the `Account`, access/gas keys, contract code, and contract data — but never scans or removes `TrieKey::PostponedReceipt`, `PostponedReceiptId`, or `PendingDataCount` entries for that account. When the account is later recreated under the same `account_id` and the postponed receipt's remaining `DataReceipt`s arrive, `process_receipt` (`runtime/runtime/src/lib.rs:1367-1474`) fetches and executes the stale postponed receipt against the *new* account fetched fresh via `get_account` inside `apply_action_receipt` (`runtime/runtime/src/lib.rs:853`).

### Finding Description
Authorization for administrative actions like `AddKey` is enforced purely by name equality in `check_actor_permissions` (`runtime/runtime/src/actions.rs:739-784`): `actor_id != account_id` → `ActorNoPermission`. `actor_id` is seeded from `receipt.predecessor_id()` at the start of `apply_action_receipt` (`runtime/runtime/src/lib.rs:855`). For a self-targeted receipt (predecessor_id == receiver_id, e.g., a contract issuing a promise batch action to itself with an `AddKeyAction`), this check only compares account-id strings — it has no notion of "the same underlying owner/keys," and no signature is re-checked at action-execution time (signatures are only validated when the original transaction created the very first receipt).

Exploit flow:
1. Attacker deploys a malicious contract on an account they control, e.g. `victim-name.parent.near` (attacker controls `parent.near`, so it can `CreateAccount` under it, and can also `DeleteAccount` on `victim-name.parent.near` since `actor_id == account_id` for a self-delete).
2. Attacker's contract, from a `FunctionCall`, issues a self-targeted promise batch action containing `Action::AddKey(FullAccess)`, and gives it two dependent `input_data_ids` (e.g., via two dangling cross-contract calls or crafted `PromiseAnd`). This receipt is postponed (`process_action_receipt`, `lib.rs:1608-1655`) and stored under `TrieKey::PostponedReceipt{receiver_id: victim-name.parent.near, receipt_id}` plus `PendingDataCount`/`PostponedReceiptId` entries, all keyed only by account name.
3. Before both `DataReceipt`s land, the attacker submits `DeleteAccount` on `victim-name.parent.near` (passes `check_actor_permissions` because predecessor == receiver). `remove_account` (`core/store/src/utils/mod.rs:504-575`) removes the `Account`, keys, code, and contract data, but leaves the `PostponedReceipt`/`PendingDataCount`/`PostponedReceiptId` rows untouched — there is no cleanup call for these trie keys anywhere in `action_delete_account`.
4. Attacker (or, more critically, a completely unrelated third party who is unaware the name was ever used and later re-registers the freed subaccount name under `parent.near`) issues `CreateAccount` for `victim-name.parent.near` again. `action_create_account` (`runtime/runtime/src/actions.rs:167-210`) has no knowledge of / does not check for pending postponed receipts against that name.
5. The two outstanding `DataReceipt`s finally arrive. `process_receipt` (`lib.rs:1367-1474`) decrements `PendingDataCount` to 0, retrieves the stale `PostponedReceipt` via `get_postponed_receipt`, and calls `apply_action_receipt`, which re-fetches the account via `get_account` (`lib.rs:853`) — now the *new* account. `check_actor_permissions` still passes because `actor_id` (the original predecessor, still literally `victim-name.parent.near`) equals `account_id` (`victim-name.parent.near`) as plain strings. `action_add_key` then installs the attacker-chosen `FullAccess` public key onto the new account with zero involvement of the new owner's signature.

Existing checks that do NOT stop this: nonce/signature checks (only apply to the original transaction, not to postponed-receipt replays), storage-staking checks (irrelevant), and `check_actor_permissions` (defeated because it is name-based, not identity-based, and the account's identity/key-material history is not part of the comparison).

### Impact Explanation
This is an authorization escalation across accounts: an attacker plants a `FullAccess` (or other privileged) access key on any future occupant of a reused account name, without any signature or consent from that occupant, purely by scheduling a postponed receipt before self-deleting the account. This directly matches the "authorization escalation across accounts or promises" bounty category and can lead to full compromise/fund theft of any account later created under a name the attacker previously controlled and abandoned via `DeleteAccount`. The severity is compounded by the fact that account-name reuse is otherwise an accepted/known feature of NEAR's naming model (subaccounts can be freely recreated by the parent), while the postponed-receipt survival is not part of documented behavior and appears to be a genuine bookkeeping gap in `remove_account`.

### Likelihood Explanation
Preconditions are fully within an ordinary unprivileged attacker's control: deploy an arbitrary contract, issue a `FunctionCall` from it that creates a two-dependency postponed self-receipt with an embedded `AddKeyAction`, then submit a `DeleteAccount` transaction before the dependent `DataReceipt`s resolve, and finally have some future party (or the attacker themself, if they still control the parent account and can recreate the child) recreate the account name. Achieving the necessary timing (posting `DeleteAccount` in a block after the postponing receipt but before both dangling data receipts resolve) is straightforward because the attacker fully controls the timing of the two callbacks that produce the missing `input_data_ids` (e.g., by having a helper contract simply never call back, and manually triggering the resolving `DataReceipt` cross-shard calls after account recreation). Cost is a handful of function calls and one delete/create cycle — cheap and fully repeatable against any account name the attacker can create and delete (i.e., any subaccount under a namespace they control).

### Recommendation
When `action_delete_account`/`remove_account` deletes an account, also scan and remove all `TrieKey::PostponedReceipt`, `TrieKey::PostponedReceiptId`, `TrieKey::PendingDataCount`, and `TrieKey::ReceivedData` entries whose `receiver_id` equals the deleted account (mirroring the existing access-key/contract-data prefix-iteration pattern already used in `remove_account`, `core/store/src/utils/mod.rs:515-573`). Alternatively/additionally, refuse to postpone a receipt whose actions include self-administrative actions (`AddKey`/`DeleteKey`/`Stake`/`DeployContract`) unless the account's identity is re-validated (e.g., by binding the postponed receipt to a generation/version counter incremented on every `DeleteAccount`+`CreateAccount` cycle, and rejecting execution if the counter has changed since postponement).

### Proof of Concept
Runtime `tests/apply.rs`-style integration test (following the pattern of `test_promise_input_size_limit_exceeded_fails_and_cleans_up` and `test_function_call_after_same_chunk_delete_recreate_resolves_fresh_code` already in `runtime/runtime/src/tests/apply.rs`):
1. Create account `child.alice.near` owned by "attacker" key A.
2. Submit an `ActionReceipt` to `child.alice.near` with predecessor `child.alice.near`, `input_data_ids = [d1, d2]`, and `actions = [Action::AddKey(pubkey_evil, FullAccess)]`. Assert it is stored as `PostponedReceipt` (via `get_postponed_receipt`) and not yet applied.
3. Submit a `DeleteAccount{beneficiary_id: alice}` receipt with predecessor == receiver == `child.alice.near`. Assert it succeeds and the account is gone (`get_account` returns `None`).
4. Submit `CreateAccount` for `child.alice.near` with predecessor `alice.near`, followed by `AddKey(pubkey_new_owner)` in the same or later receipt, simulating a new legitimate owner.
5. Deliver the two `DataReceipt`s for `d1`, `d2` targeting `child.alice.near`.
6. Assert: (a) the postponed receipt executes (no `StorageInconsistentState` error, confirming it wasn't cleaned up); (b) `pubkey_evil` is now present as a `FullAccess` key on `child.alice.near`, added without any signature from the new owner — demonstrating the escalation; (c) compare against expected/fixed behavior where the postponed receipt should instead fail or be purged after account deletion. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** runtime/runtime/src/lib.rs (L850-861)
```rust
            receipt_hash: receipt.get_hash(),
        });

        let mut account = get_account(state_update, account_id)?;
        let account_did_not_exist = account.is_none();
        let mut actor_id = receipt.predecessor_id().clone();
        let mut result = ActionReceiptResult::new();
        let exec_fees = apply_state.config.fees.fee(ActionCosts::new_action_receipt).exec_fee();
        result.gas_used = exec_fees.gas;
        result.gas_burnt = exec_fees.gas;
        result.compute_usage = exec_fees.compute;

```

**File:** runtime/runtime/src/lib.rs (L1367-1474)
```rust
    fn process_receipt(
        &self,
        processing_state: &mut ApplyProcessingReceiptState,
        receipt: &Receipt,
        receipt_sink: &mut ReceiptSink,
        validator_proposals: &mut Vec<ValidatorStake>,
    ) -> Result<Option<ExecutionOutcomeWithId>, RuntimeError> {
        let ApplyProcessingReceiptState {
            ref mut state_update,
            apply_state,
            epoch_info_provider,
            ref pipeline_manager,
            ref mut stats,
            ref mut instant_receipts,
            ref mut receipt_to_tx,
            ..
        } = *processing_state;
        let account_id = receipt.receiver_id();
        match receipt.versioned_receipt() {
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
                    } else {
                        // There is still some pending data for the receipt, so we update the
                        // pending data count in the state.
                        set(
                            state_update,
                            TrieKey::PendingDataCount {
                                receiver_id: account_id.clone(),
                                receipt_id,
                            },
                            &(pending_data_count.checked_sub(1).ok_or_else(|| {
                                StorageError::StorageInconsistentState(
                                    "pending data count is 0, but there is a new DataReceipt"
                                        .to_string(),
                                )
                            })?),
                        );
                    }
                }
            }
```

**File:** runtime/runtime/src/lib.rs (L1593-1658)
```rust
    fn process_action_receipt(
        &self,
        receipt: &Receipt,
        receipt_sink: &mut ReceiptSink,
        instant_receipts: &mut VecDeque<Receipt>,
        validator_proposals: &mut Vec<ValidatorStake>,
        state_update: &mut TrieUpdate,
        apply_state: &ApplyState,
        epoch_info_provider: &dyn EpochInfoProvider,
        pipeline_manager: &ReceiptPreparationPipeline,
        stats: &mut ChunkApplyStatsV1,
        account_id: &AccountId,
        action_receipt: VersionedActionReceipt<'_>,
        receipt_to_tx: &mut Vec<(CryptoHash, ReceiptToTxInfo)>,
    ) -> Result<Option<ExecutionOutcomeWithId>, RuntimeError> {
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

        Ok(None)
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

**File:** runtime/runtime/src/actions.rs (L739-784)
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
```
