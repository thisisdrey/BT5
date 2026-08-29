### Title
Incomplete state cleanup on account deletion: `remove_account` leaks `ReceivedData`, `PostponedReceipt(Id)`, `PendingDataCount`, `PromiseYieldReceipt/Status`, and `YieldId↔DataId` entries, enabling receipt confusion after account-name reuse - ([File: core/store/src/utils/mod.rs])

### Summary
`remove_account` only deletes the `Account`, `ContractCode`, access keys/gas-key nonces, and `ContractData` trie entries for a deleted account; it does not iterate/clear `col::RECEIVED_DATA`, `col::POSTPONED_RECEIPT`, `col::POSTPONED_RECEIPT_ID`, `col::PENDING_DATA_COUNT`, `PromiseYieldReceipt`/`PromiseYieldStatus`, or the `YieldIdToDataId`/`DataIdToYieldId` mappings, even though `trie_key_parsers` exposes the parsing needed to do so generically (as it already does for access keys and contract data). Note: the function actually lives in `core/store/src/utils/mod.rs`, not `runtime/runtime/src/lib.rs` as stated in the question — the file reference in the premise is inaccurate, but the code behavior it describes is real.

### Finding Description
`remove_account` (`core/store/src/utils/mod.rs:505-575`) is invoked from `action_delete_account` (`runtime/runtime/src/actions.rs:314-390`), which is reachable from an ordinary `DeleteAccount` action signed by the account owner (or a self-issued promise, as shown by the instant-receipt path in `Receipt::is_instant_receipt`, `core/primitives/src/receipt.rs:474-491`, and exercised in `test-loop-tests/src/tests/create_delete_account.rs`). The function only:
- removes `TrieKey::Account` and `TrieKey::ContractCode` (lines 509-510),
- iterates and removes access keys/gas key nonces via `get_raw_prefix_for_access_keys` (lines 516-553),
- iterates and removes `ContractData` via `get_raw_prefix_for_contract_data` (lines 556-573).

It never touches `TrieKey::ReceivedData`, `TrieKey::PostponedReceiptId`, `TrieKey::PendingDataCount`, `TrieKey::PostponedReceipt`, `TrieKey::PromiseYieldReceipt`, `TrieKey::PromiseYieldStatus`, `TrieKey::YieldIdToDataId`, or `TrieKey::DataIdToYieldId` — all of which are keyed only by `account_id` (`core/primitives/src/trie_key.rs:203-234`), with no per-account "incarnation" discriminator. `action_delete_account` itself has no precondition check for outstanding postponed/yielded receipts for the account (only storage-size and gas-key-balance checks, `runtime/runtime/src/actions.rs:326-363`), and account-name reuse is not blocked after deletion (`CreateAccount`/implicit creation only checks the account currently doesn't exist).

Exploit flow: an attacker-controlled account `A` receives a cross-shard action receipt with unmet `input_data_ids`, causing the runtime to persist `PostponedReceiptId`, `PendingDataCount`, and `PostponedReceipt` for `A` (`runtime/runtime/src/lib.rs:1609-1655`, `process_action_receipt`). Before the missing `Data` receipt arrives, the attacker (owning `A`) self-deletes `A` via `DeleteAccount`. The postponed-receipt bookkeeping for `A` is never cleared. The attacker (or anyone) then recreates an account named `A`. When the originally in-flight `Data` receipt eventually arrives, `process_receipt`'s `Data` arm (`runtime/runtime/src/lib.rs:1386-1473`) looks up `PostponedReceiptId`/`PendingDataCount`/`PostponedReceipt` keyed purely by `account_id = A` — finds the stale entries belonging to the deleted incarnation — and executes that old postponed action receipt against the new account `A`, which never issued or consented to it. The same confusion applies to `PromiseYieldReceipt`/`PromiseYieldStatus`/`YieldIdToDataId` for yielded promises resumed after deletion+recreation.

### Impact Explanation
This is an authorization-escalation / receipt-confusion issue: actions from a stale, logically-distinct account incarnation execute against a newly created account of the same name without its owner's consent. Depending on the contents of the stale postponed/promise-yield receipt, this can move funds, mutate contract state, or resume a yielded promise unexpectedly on the new account — matching the "authorization escalation across accounts or promises" bounty category. It also causes unbounded orphaned state growth (garbage trie entries with no paying owner), a secondary state-bloat concern.

### Likelihood Explanation
Preconditions are fully attacker-controlled and require no privileged access: deploy a contract, generate a cross-shard action receipt with unmet input data targeting an owned account, delete that account before the data arrives, then recreate an account with the same name. This is repeatable and low-cost (pure gas/storage cost of a few transactions), though it requires precise timing relative to the arrival of the missing `Data`/`PromiseResume` receipt, which the attacker can engineer since they control both the yielding and resuming call graph.

### Recommendation
Extend `remove_account` to also enumerate and delete, using the existing `trie_key_parsers::parse_account_id_from_trie_key_with_separator`-style generic parsing, all remaining `col::COLUMNS_WITH_ACCOUNT_ID_IN_KEY` prefixes for the account: `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, `DataIdToYieldId`. Alternatively/additionally, block `DeleteAccount` while any such pending entries exist for the account (analogous to the existing staking-lock check), forcing outstanding cross-shard state to resolve or expire before deletion is permitted.

### Proof of Concept
Unit test in `core/store/src/utils/mod.rs` (or a runtime/test-loop integration test):
1. For each column in `col::COLUMNS_WITH_ACCOUNT_ID_IN_KEY`, write one row keyed by a target `account_id` (e.g. set a `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `ReceivedData`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, `DataIdToYieldId`).
2. Call `remove_account(&mut state_update, &account_id)`.
3. For each column, iterate the raw trie by that column's account prefix and assert no rows remain.
Expected: assertion fails for `RECEIVED_DATA`, `POSTPONED_RECEIPT_ID`, `PENDING_DATA_COUNT`, `POSTPONED_RECEIPT`, `PROMISE_YIELD_RECEIPT`, `PROMISE_YIELD_STATUS`, `YIELD_ID_TO_DATA_ID`, `DATA_ID_TO_YIELD_ID`, proving the leftover-state defect.

Integration-level PoC (test-loop): create account `A`, trigger a postponed action receipt to `A` with an unmet `input_data_id`, delete `A`, recreate `A`, then deliver the missing `Data` receipt; assert that the recreated `A`'s state is mutated by the resurrected postponed receipt despite `A`'s new owner never authorizing it. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** core/primitives/src/trie_key.rs (L203-234)
```rust
    /// purposes to avoid deserializing the entire receipt.
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
    /// Used to store indices of the delayed receipts queue (`node-runtime::DelayedReceiptIndices`).
    /// NOTE: It is a singleton per shard.
    DelayedReceiptIndices = col::DELAYED_RECEIPT_OR_INDICES,
    /// Used to store a delayed receipt `primitives::receipt::Receipt` for a given index `u64`
    /// in a delayed receipt queue. The queue is unique per shard.
    DelayedReceipt {
        index: u64,
    } = 8,
    /// Used to store a key-value record `Vec<u8>` within a contract deployed on a given `AccountId`
    /// and a given key.
    ContractData {
        account_id: AccountId,
        key: Vec<u8>,
    } = col::CONTRACT_DATA,
    /// Used to store head and tail indices of the PromiseYield timeout queue.
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

**File:** runtime/runtime/src/lib.rs (L1367-1473)
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

**File:** core/primitives/src/receipt.rs (L468-491)
```rust
    /// An instant receipt is a receipt which should be processed immediately after the receipt that
    /// produced it, in the same chunk, irrespective of the gas limit.
    /// The expectation is that applying an instant receipt is a quick operation (e.g. setting a few values in the state).
    /// Instant receipts generally shouldn't emit new instant receipts, as it could lead to
    /// infinitely many receipts being executed in a single chunk.
    pub fn is_instant_receipt(&self) -> bool {
        match self.versioned_receipt() {
            VersionedReceiptEnum::PromiseYield(_) => {
                // PromiseYield receipts are instant receipts.
                // Applying a PromiseYield receipt is one trie write, it's okay to make it an instant receipt.
                true
            }
            VersionedReceiptEnum::Action(action_receipt) => {
                // Action receipts containing a single DeleteAccount action and no input
                // promises are instant receipts.
                // Deleting an account is a quick trie operation, it's okay to make it instant.
                matches!(action_receipt.actions(), [Action::DeleteAccount(_)])
                    && action_receipt.input_data_ids().is_empty()
            }
            VersionedReceiptEnum::Data(_)
            | VersionedReceiptEnum::PromiseResume(_)
            | VersionedReceiptEnum::GlobalContractDistribution(_) => false,
        }
    }
```
