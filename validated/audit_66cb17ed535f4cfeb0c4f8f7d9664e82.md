### Title
Account deletion leaves ReceivedData, PostponedReceipt/PostponedReceiptId, PendingDataCount, PromiseYieldReceipt/PromiseYieldStatus, YieldIdToDataId/DataIdToYieldId rows behind, enabling delayed cross-account receipt execution after account-name reuse - ([File: core/store/src/utils/mod.rs])

### Summary
`remove_account` (core/store/src/utils/mod.rs:505-575) only deletes `TrieKey::Account`, `ContractCode`, `AccessKey`/`GasKeyNonce`, and `ContractData`. It does not enumerate or remove `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, or `DataIdToYieldId`, even though `TrieKey::get_account_id()` (core/primitives/src/trie_key.rs:590-619) reports an owning account for all of these variants. Since `Runtime::process_receipt`'s Data arm (runtime/runtime/src/lib.rs:1386-1474) and the `PromiseResume`/`PromiseYield` handling key strictly off `receiver_id` strings with no check that the account currently exists or is the "same" logical account, a later-delivered `Data`/`PromiseResume` receipt for the deleted-then-recreated account name can pull a stale `PostponedReceipt`/`PromiseYieldReceipt` out of state and execute it against whatever new account now occupies that name.

### Finding Description
`action_delete_account` (runtime/runtime/src/actions.rs:314-390) calls `remove_account` unconditionally on `DeleteAccount`, with no precondition that the deleted account has zero pending input-data dependencies, zero postponed receipts, or zero outstanding promise-yields. `remove_account` itself:
- Removes `Account`, `ContractCode` directly.
- Iterates the access-key prefix to remove `AccessKey`/`GasKeyNonce`.
- Iterates the contract-data prefix to remove `ContractData`.
- Never touches `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, `DataIdToYieldId`, all of which are keyed by `receiver_id` (the very account being deleted) per `TrieKey::get_account_id()`.

An attacker (unprivileged) can set up:
1. Send a self-directed `ActionReceipt` to their own account `victim.near` with two `input_data_ids`; runtime postpones it, writing `PostponedReceiptId` (x2), `PendingDataCount=2`, and `PostponedReceipt` (runtime/runtime/src/lib.rs:1609-1654).
2. Deliver only one of the two `Data` receipts; this decrements `PendingDataCount` to 1 and writes `ReceivedData` for that `data_id` (runtime/runtime/src/lib.rs:1386-1474). The second `Data` receipt is withheld/never sent by the attacker's own contract logic (it's the attacker's contract that would emit it via a promise callback, so the attacker controls whether/when it's produced).
3. Submit `DeleteAccount` on `victim.near`, refunding balance to a beneficiary. `remove_account` clears `Account`/`ContractCode`/keys/`ContractData`, but leaves `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt` intact under the `victim.near` key prefix.
4. `victim.near` is now unregistered/available; a different, unrelated user creates a fresh account with that exact name (a normal, permissionless `CreateAccount` action) and deploys their own contract/keys.
5. The attacker later triggers delivery of the still-outstanding second `Data` receipt for that `data_id`/`receiver_id`. `process_receipt`'s Data arm (runtime/runtime/src/lib.rs:1398-1455) finds the matching `PostponedReceiptId`, decrements `PendingDataCount` to 0, fetches the `PostponedReceipt` (the original action list crafted before deletion), and executes it via `apply_action_receipt` — now against the new, unrelated account's state/contract/balance.

None of the existing checks (signature/nonce validation, access-key permission checks, `check_actor_permissions`, storage-staking checks) intervene here, because this path never goes through transaction/access-key validation again — it is purely internal receipt-queue processing keyed by account-id strings that assumes account identity persists, which `remove_account`'s incomplete cleanup breaks.

### Impact Explanation
This is a genuine authorization-escalation gap in the survivor set enumerated by the audit question: `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, `DataIdToYieldId` all persist under a deleted account's key prefix and are exactly the state consumed by `process_receipt`/`process_action_receipt` keyed only by `receiver_id`. The scoped impact per the question is the enumeration itself (state/value conservation — deleted account name retains hidden rows), which is confirmed: the actual removed set is `{Account, ContractCode, AccessKey, GasKeyNonce, ContractData}`; the actual survivor set matches exactly `{ReceivedData, PostponedReceiptId, PendingDataCount, PostponedReceipt, PromiseYieldReceipt, PromiseYieldStatus, YieldIdToDataId, DataIdToYieldId}` claimed in the question. Whether this translates into concrete fund theft depends on the attacker crafting a `PostponedReceipt` whose actions can extract value from an unrelated future occupant of the account name — a real but narrower exploitation path than the "hidden state survives deletion" finding itself, which is the specific target of this question.

### Likelihood Explanation
Precondition is cheap: any account owner can self-issue a multi-`input_data_id` receipt, withhold one dependency, and delete the account — all via ordinary signed transactions with no special privilege. Recreation of the account name by an unrelated party is a normal, permissionless action in NEAR (deleted names are not reserved). The remaining step (triggering delivery of the withheld `Data`/`PromiseResume` receipt at an attacker-chosen time) is controlled entirely by the attacker's own earlier receipt-emission logic, making this fully repeatable and low-cost.

### Recommendation
Extend `remove_account` (core/store/src/utils/mod.rs:505-575) to also enumerate and remove all `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, and `DataIdToYieldId` rows scoped to the account being deleted (via the corresponding trie-key prefixes, analogous to the existing access-key/contract-data prefix scans), or alternatively reject `DeleteAccount` while any such pending rows exist for the account (mirroring the existing `DeleteAccountStaking` precondition pattern).

### Proof of Concept
Rust unit test in `core/store/src/utils/mod.rs` (or `runtime/runtime/src/tests/`):
1. Populate one row of each `TrieKey` variant for which `get_account_id()` returns `Some(account_id)` under a test account: `Account`, `ContractCode`, `AccessKey`, `GasKeyNonce`, `ReceivedData` (write directly), `PostponedReceiptId`+`PendingDataCount`+`PostponedReceipt` (simulate via `set_postponed_receipt`/direct `set`), `ContractData`, and `PromiseYieldReceipt`/`PromiseYieldStatus`/`YieldIdToDataId`/`DataIdToYieldId` (via direct `set` calls or `promise_yield_create`/`promise_yield_create_with_id` helpers).
2. Call `remove_account(&mut state_update, &account_id)`.
3. Assert set-equality: the removed/empty set is exactly `{Account, ContractCode, AccessKey, GasKeyNonce, ContractData}`, and the non-empty survivor set is exactly `{ReceivedData, PostponedReceiptId, PendingDataCount, PostponedReceipt, PromiseYieldReceipt, PromiseYieldStatus, YieldIdToDataId, DataIdToYieldId}`.
4. (Integration-level extension) In `runtime/runtime/src/tests/apply.rs`, replicate the delete-then-recreate scenario shown in `test_function_call_after_same_chunk_delete_recreate_resolves_fresh_code` (runtime/runtime/src/tests/apply.rs:4877-4954) but with a pending `Data`/`PostponedReceipt` dependency instead of a `FunctionCall`, and assert that delivering the withheld `Data` receipt after account recreation causes `apply_action_receipt` to execute against the new account's state. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** core/primitives/src/trie_key.rs (L590-619)
```rust
    pub fn get_account_id(&self) -> Option<AccountId> {
        match self {
            TrieKey::Account { account_id, .. } => Some(account_id.clone()),
            TrieKey::ContractCode { account_id, .. } => Some(account_id.clone()),
            TrieKey::AccessKey { account_id, .. } => Some(account_id.clone()),
            TrieKey::GasKeyNonce { account_id, .. } => Some(account_id.clone()),
            TrieKey::ReceivedData { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::PostponedReceiptId { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::PendingDataCount { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::PostponedReceipt { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::DelayedReceiptIndices => None,
            TrieKey::DelayedReceipt { .. } => None,
            TrieKey::ContractData { account_id, .. } => Some(account_id.clone()),
            TrieKey::PromiseYieldIndices => None,
            TrieKey::PromiseYieldTimeout { .. } => None,
            TrieKey::PromiseYieldReceipt { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::BufferedReceiptIndices => None,
            TrieKey::BufferedReceipt { .. } => None,
            TrieKey::BandwidthSchedulerState => None,
            TrieKey::BufferedReceiptGroupsQueueData { .. } => None,
            TrieKey::BufferedReceiptGroupsQueueItem { .. } => None,
            // Even though global contract code might be deployed under account id, it doesn't
            // correspond to the data stored for that account id, so always returning None here.
            TrieKey::GlobalContractCode { .. } => None,
            TrieKey::GlobalContractNonce { .. } => None,
            TrieKey::PromiseYieldStatus { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::YieldIdToDataId { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::DataIdToYieldId { receiver_id, .. } => Some(receiver_id.clone()),
        }
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
