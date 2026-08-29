### Title
`remove_account` fails to purge receipt/yield-queue state, allowing a recreated account to inherit stale cross-account data - (`core/store/src/utils/mod.rs`)

### Summary
`remove_account` only deletes `TrieKey::Account`, `TrieKey::ContractCode`, `TrieKey::AccessKey`/`TrieKey::GasKeyNonce`, and `TrieKey::ContractData` for the target account. [1](#0-0)  It never touches `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, or `DataIdToYieldId`, even though all eight are keyed by the account's `receiver_id`/`account_id` per `TrieKey`. [2](#0-1) [3](#0-2)  Since account names can be deleted and later recreated under the same `AccountId`, this leftover state can be picked up and acted upon by the newly-created account.

### Finding Description
`action_delete_account` calls `remove_account(state_update, account_id)` as the sole cleanup step when a `DeleteAccount` action executes. [4](#0-3)  Tracing `remove_account`:
- It removes `Account`, `ContractCode` (line 509-510), iterates and removes `AccessKey`/`GasKeyNonce` entries (lines 515-553), and iterates and removes `ContractData` entries (lines 555-573). [5](#0-4) 
- It never calls `state_update.remove` for `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, or `DataIdToYieldId`.

These entries are all written keyed by `account_id`/`receiver_id` (`set_received_data`, `set_postponed_receipt`, `set_promise_yield_receipt`, `set_promise_yield_status`, `set_yield_id_mapping`, and the raw `set`/`state_update.remove` calls for `PostponedReceiptId`/`PendingDataCount` in `process_receipt`). [6](#0-5) [7](#0-6)  Because NEAR allows an account name to be deleted and later recreated (e.g., a sub-account fully controlled by its own creator via `CreateAccount`), any such stale record persists in the trie under the same key and is transparently read back once the name is reused.

Concretely: `process_receipt` for a `Data` receipt writes `ReceivedData` and then checks `PostponedReceiptId`/`PendingDataCount` to decide whether to fetch and execute a previously postponed action receipt (`get_postponed_receipt`, `remove_postponed_receipt`, then `apply_action_receipt`) — purely keyed on `receiver_id`, with no check that the account existed continuously since the receipt was postponed. [8](#0-7)  If account `A` is deleted while it has an outstanding postponed receipt (input data still pending), `remove_account` leaves `PostponedReceiptId`, `PendingDataCount`, and `PostponedReceipt` in place. If `A` is later recreated (trivial for a sub-account the attacker fully owns) and the originally-awaited `Data` receipt eventually arrives, the runtime will silently resurrect and execute the old postponed action receipt against the *new* incarnation of `A`, running whatever actions it contains (transfers, function calls, etc.) in a context the new account owner never approved. The analogous cross-account leakage applies to `PromiseYieldReceipt`/`PromiseYieldStatus`/`YieldIdToDataId`/`DataIdToYieldId` for `promise_yield`-based callbacks, and to `ReceivedData` sitting around to satisfy a future postponed receipt.

No existing check (signature, nonce, access-key permission, storage staking) guards against this because deletion is authorized by design (`action_delete_account`'s only checks are storage-size and gas-key-balance limits), and the actor never needs to prove the leftover keys are empty.

### Impact Explanation
This is an **authorization escalation across accounts/promises**: a receipt or yielded-promise callback originally scoped to one account's execution can execute against an unrelated later account that happens to reuse the same `AccountId`. Depending on the postponed receipt's actions (e.g., `Transfer`, `FunctionCall`, `AddKey`), this can move funds unexpectedly out of, or execute privileged logic against, the recreated account — a fund-theft/state-integrity issue, not merely storage hygiene, matching NEAR's "authorization escalation across accounts or promises" bounty category.

### Likelihood Explanation
Fully attacker-controlled and repeatable with no validator/node privileges: the attacker only needs to (1) own a sub-account, (2) trigger a cross-contract call so a postponed receipt / promise-yield entry is created for it, (3) call `DeleteAccount` on that sub-account before the pending data arrives, (4) recreate the same sub-account name, and (5) let/force the originally-awaited data receipt arrive. All of these are ordinary transaction submissions costing only gas/storage deposits, and the sequence can be repeated deterministically. Preconditions (holding a pending cross-contract call at deletion time) are easy to engineer, e.g. call a slow/never-responding external receiver deliberately, then delete before it resolves.

### Recommendation
Extend `remove_account` in `core/store/src/utils/mod.rs` to also enumerate and remove `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, and `DataIdToYieldId` entries scoped to `account_id`, mirroring the existing prefix-iteration pattern already used for access keys and contract data (using `trie_key_parsers` prefix helpers for each column), before allowing `DeleteAccountAction` to complete. Alternatively/additionally, reject account recreation while any such orphaned keys still exist for that `AccountId`.

### Proof of Concept
Add a differential unit test in `core/store/src/utils/mod.rs` (or a `runtime/runtime` test-loop test):
1. Create `TrieUpdate`/`Trie` fixture and account `A`.
2. Call every setter for the 13 `TrieKey` variants scoped to `A`: `set_account`, `set(state_update, TrieKey::ContractCode{...}, ...)`, `set_access_key`, `set_gas_key_nonce`, `set(state_update, TrieKey::ContractData{...}, ...)`, `set_received_data`, `set(state_update, TrieKey::PostponedReceiptId{...}, ...)`, `set(state_update, TrieKey::PendingDataCount{...}, ...)`, `set_postponed_receipt`, `set_promise_yield_receipt`, `set_promise_yield_status`, `set_yield_id_mapping` (writes both `YieldIdToDataId` and `DataIdToYieldId`).
3. Commit/finalize the `TrieUpdate` so the writes are visible to iteration, then call `remove_account(&mut state_update, &A)`.
4. For each of the 13 keys, assert `trie.get(&key, AccessOptions::DEFAULT)? == None`.

Expected result: assertions **pass** for `Account`, `ContractCode`, `AccessKey`, `GasKeyNonce`, `ContractData` (5 variants), and **fail** for `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, `DataIdToYieldId` (8 variants) — because `remove_account`'s implementation contains no code path removing them. [9](#0-8)

### Citations

**File:** core/store/src/utils/mod.rs (L76-83)
```rust
pub fn set_received_data(
    state_update: &mut TrieUpdate,
    receiver_id: AccountId,
    data_id: CryptoHash,
    data: &ReceivedData,
) {
    set(state_update, TrieKey::ReceivedData { receiver_id, data_id }, data);
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

**File:** core/primitives/src/trie_key.rs (L196-219)
```rust
    ReceivedData {
        receiver_id: AccountId,
        data_id: CryptoHash,
    } = col::RECEIVED_DATA,
    /// Used to store receipt ID `primitives::hash::CryptoHash` for a given receiver's `AccountId`
    /// of the receipt and a given `data_id` (the unique identifier for the required input data).
    /// NOTE: This receipt ID indicates the postponed receipt. We store `receipt_id` for performance
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
```

**File:** core/primitives/src/trie_key.rs (L242-293)
```rust
    /// Used to store the postponed promise yield receipt `primitives::receipt::Receipt`
    /// for a given receiver's `AccountId` and a given `data_id`.
    PromiseYieldReceipt {
        receiver_id: AccountId,
        data_id: CryptoHash,
    } = col::PROMISE_YIELD_RECEIPT,
    /// Used to store indices of the buffered receipts queues per shard.
    /// NOTE: It is a singleton per shard, holding indices for all outgoing shards.
    BufferedReceiptIndices = col::BUFFERED_RECEIPT_INDICES,
    /// Used to store a buffered receipt `primitives::receipt::Receipt` for a
    /// given index `u64` and receiving shard. There is one unique queue
    /// per ordered shard pair. The trie for shard X stores all queues for pairs
    /// (X,*) without (X,X).
    BufferedReceipt {
        receiving_shard: ShardId,
        index: u64,
    } = col::BUFFERED_RECEIPT,
    BandwidthSchedulerState = col::BANDWIDTH_SCHEDULER_STATE,
    /// Stores `ReceiptGroupsQueueData` for the receipt groups queue
    /// which corresponds to the buffered receipts to `receiver_shard`.
    BufferedReceiptGroupsQueueData {
        receiving_shard: ShardId,
    } = col::BUFFERED_RECEIPT_GROUPS_QUEUE_DATA,
    /// A single item of `ReceiptGroupsQueue`. Values are of type `ReceiptGroup`.
    BufferedReceiptGroupsQueueItem {
        receiving_shard: ShardId,
        index: u64,
    } = col::BUFFERED_RECEIPT_GROUPS_QUEUE_ITEM,
    GlobalContractCode {
        identifier: GlobalContractCodeIdentifier,
    } = col::GLOBAL_CONTRACT_CODE,
    /// Global contract deployment nonce. Stores the nonce of the last
    /// deployment for nonce-based idempotency during distribution.
    GlobalContractNonce {
        identifier: GlobalContractCodeIdentifier,
    } = col::GLOBAL_CONTRACT_NONCE,
    PromiseYieldStatus {
        receiver_id: AccountId,
        data_id: CryptoHash,
    } = col::PROMISE_YIELD_STATUS,
    /// Mapping from user-provided yield ID to runtime-generated data ID.
    /// Used by `promise_yield_create_with_id` for duplicate detection.
    YieldIdToDataId {
        receiver_id: AccountId,
        yield_id: YieldId,
    } = col::YIELD_ID_TO_DATA_ID,
    /// Reverse mapping from runtime-generated data ID to user-provided yield ID.
    /// Used to clean up `YieldIdToDataId` when a yield is resumed or times out.
    DataIdToYieldId {
        receiver_id: AccountId,
        data_id: CryptoHash,
    } = col::DATA_ID_TO_YIELD_ID,
```

**File:** runtime/runtime/src/actions.rs (L371-371)
```rust
    let remove_result = remove_account(state_update, account_id)?;
```

**File:** runtime/runtime/src/lib.rs (L1386-1455)
```rust
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
```
