### Title
Account deletion (`remove_account`) leaves orphaned `ReceivedData`/`PostponedReceipt*`/`PromiseYield*`/`YieldIdToDataId`/`DataIdToYieldId` trie rows, enabling stale-receipt replay against a recreated account of the same name - ([File: core/store/src/utils/mod.rs])

### Summary
`remove_account` (`core/store/src/utils/mod.rs:505-575`) removes only `Account`, `ContractCode`, `AccessKey`/`GasKeyNonce`, and `ContractData` rows for the target `account_id`. It never touches `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, or `DataIdToYieldId`, even though `TrieKey::get_account_id()` (`core/primitives/src/trie_key.rs:590-619`) reports all of these as belonging to that account. Because NEAR permits re-registering an account name after deletion (a user fully controls creation/deletion of its own sub-accounts), these orphaned rows persist in state under the reused name and can later be picked up by the runtime's postponed-receipt/yield-resolution logic, executing stale receipt data against the new account incarnation.

### Finding Description
`action_delete_account` (`runtime/runtime/src/actions.rs:314-390`) unconditionally calls `remove_account` when a `DeleteAccountAction` executes; it does not check whether the account has outstanding postponed receipts, received-data entries, or live promise-yield state before wiping the `Account`/`ContractCode`/`AccessKey`/`ContractData` rows.

`remove_account` (`core/store/src/utils/mod.rs:509-573`) only:
- removes `TrieKey::Account` and `TrieKey::ContractCode` directly,
- iterates the access-key prefix to remove `AccessKey`/`GasKeyNonce` rows,
- iterates the contract-data prefix to remove `ContractData` rows.

It has no iteration/removal step for the received-data, postponed-receipt, or promise-yield key spaces, even though `TrieKey::get_account_id()` (`core/primitives/src/trie_key.rs:596-599,605,615-617`) explicitly attributes `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, and `DataIdToYieldId` to the `receiver_id`/`account_id` that owns them.

Exploit flow (attacker fully in control, no privileged access required):
1. Attacker creates `sub.attacker.near` (a sub-account it owns) and deploys a contract.
2. From that contract, attacker issues a cross-contract call that creates a `PostponedReceiptId`/`PendingDataCount`/`PostponedReceipt` entry (an action receipt to `sub.attacker.near` with an unmet `input_data_id`), and/or calls `promise_yield_create`/`promise_yield_create_with_id` to populate `PromiseYieldReceipt`, `PromiseYieldStatus`, and `YieldIdToDataId`/`DataIdToYieldId` rows (`runtime/runtime/src/ext.rs:353-400`, `lib.rs:1495-1499`).
3. Before the corresponding `Data`/`PromiseResume` receipt arrives, attacker submits a separate receipt containing only `DeleteAccountAction` targeting `sub.attacker.near` (this is an "instant receipt" per `core/primitives/src/receipt.rs:473-486` and executes independently of the still-pending data dependency). `action_delete_account` → `remove_account` runs, deleting the account object but leaving the above rows behind.
4. Attacker recreates `sub.attacker.near` with new keys/contract.
5. When the original delayed `Data`/`PromiseResume` receipt finally arrives (its `data_id` is one the attacker itself generated and remembers, since it created the yield/postponed dependency), the runtime looks up the stale `PostponedReceiptId`/`PromiseYieldReceipt` under `sub.attacker.near` (`runtime/runtime/src/lib.rs:1500-1562`, `schedule_contract_preparation` at `lib.rs:3306-3330`) and executes the old, orphaned receipt against the newly created account.

This is exactly the authorization-escalation surface the audit question targets: state (postponed receipts, yield status, id mappings) that was semantically scoped to a now-deleted account identity persists and becomes attributable to a different account instance that happens to reuse the same name.

### Impact Explanation
This falls under authorization escalation across promises: an action receipt / yield-callback created in the context of one account "incarnation" (with its own contract code, keys, and intent) executes later against a different account incarnation with the same name but potentially different code/owner. The direct diff (comparing `TrieKey::get_account_id()`'s 13 variants against the 5 actually removed by `remove_account`) confirms exactly 8 survivor variants: `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, `DataIdToYieldId`. No signature/nonce/access-key check exists to prevent this because these queue entries are keyed purely on `account_id`/`data_id`, not tied to any liveness check of the account or its creation generation.

### Likelihood Explanation
The attacker only needs to control a sub-account it can create, populate with a pending cross-shard/data dependency or yield, delete via an "instant" `DeleteAccountAction` receipt before the dependency resolves, and recreate the account name — all standard, unprivileged actions (deploy, function call, delete account, create account) available to any funded NEAR account. No validator/node privilege or race against consensus timing beyond ordinary receipt scheduling is required; the account owner fully controls when to submit the resolving `Data`/`PromiseResume` receipt.

### Recommendation
Extend `remove_account` to also iterate and remove `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, and `DataIdToYieldId` rows scoped to the deleted `account_id` before allowing the account to be recreated, or reject `DeleteAccountAction` while any such pending row exists for the account.

### Proof of Concept
Rust unit test in `core/store/src/utils/mod.rs` (or a new test module):
1. Build a `TrieUpdate` and populate one row of each of the 13 `get_account_id()`-attributable `TrieKey` variants for `account_id = "alice.near"` (using `set_account`, `set_code`, `set_access_key`, a `GasKeyNonce`, `set_received_data`, `set_postponed_receipt` + its `PostponedReceiptId`/`PendingDataCount`, `set_contract_data`, `set_promise_yield_receipt`, `set_promise_yield_status`, and `set_yield_id_mapping` for `YieldIdToDataId`/`DataIdToYieldId`).
2. Call `remove_account(&mut state_update, &"alice.near".parse().unwrap())`.
3. For each of the 13 keys, assert presence/absence: expect `Account`, `ContractCode`, `AccessKey`, `GasKeyNonce`, `ContractData` to be `None`; assert that `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, `DataIdToYieldId` are still `Some(..)` — confirming the survivor set matches the question's enumeration exactly.
4. (Integration-level, in `test-loop-tests`) reproduce the full exploit: create sub-account, start a yield/postponed dependency, delete via instant `DeleteAccountAction`, recreate the account, then deliver the original `PromiseResume`/`Data` receipt and assert it executes against the new account (mismatched contract/code hash), demonstrating cross-incarnation receipt execution. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** runtime/runtime/src/lib.rs (L1495-1562)
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
```

**File:** runtime/runtime/src/ext.rs (L353-426)
```rust
    fn create_promise_yield_receipt(
        &mut self,
        receiver_id: AccountId,
    ) -> Result<(ReceiptIndex, CryptoHash), VMLogicError> {
        let input_data_id = self.generate_data_id();
        let receipt_index =
            self.receipt_manager.create_promise_yield_receipt(input_data_id, receiver_id.clone());

        set_promise_yield_status(
            &mut self.trie_update,
            &receiver_id,
            input_data_id,
            PromiseYieldStatus::Yielded,
        );

        Ok((receipt_index, input_data_id))
    }

    fn create_promise_yield_receipt_with_id(
        &mut self,
        receiver_id: AccountId,
        user_yield_id: YieldId,
    ) -> Result<Option<(ReceiptIndex, CryptoHash)>, VMLogicError> {
        // Check for duplicate yield_id in trie. TrieUpdate also reflects writes from earlier
        // calls within the same function call, so this also catches in-transaction duplicates.
        if has_yield_id_mapping(self.trie_update, &receiver_id, user_yield_id)
            .map_err(wrap_storage_error)?
        {
            return Ok(None);
        }

        let input_data_id = self.generate_data_id();

        // Store bidirectional yield_id <-> data_id mappings
        set_yield_id_mapping(&mut self.trie_update, &receiver_id, user_yield_id, input_data_id);

        let receipt_index =
            self.receipt_manager.create_promise_yield_receipt(input_data_id, receiver_id.clone());

        set_promise_yield_status(
            &mut self.trie_update,
            &receiver_id,
            input_data_id,
            PromiseYieldStatus::Yielded,
        );

        Ok(Some((receipt_index, input_data_id)))
    }

    fn submit_promise_resume_data(
        &mut self,
        data_id: CryptoHash,
        data: Vec<u8>,
    ) -> Result<bool, VMLogicError> {
        let has_yield_receipt_in_state =
            has_promise_yield_receipt(self.trie_update, self.account_id.clone(), data_id)
                .map_err(wrap_storage_error)?;
        let has_yield_status_in_state =
            has_promise_yield_status(self.trie_update, &self.account_id, data_id)
                .map_err(wrap_storage_error)?;

        if has_yield_receipt_in_state || has_yield_status_in_state {
            self.receipt_manager.create_promise_resume_receipt(data_id, data);
            set_promise_yield_status(
                &mut self.trie_update,
                &self.account_id,
                data_id,
                PromiseYieldStatus::ResumeInitiated,
            );
            return Ok(true);
        }

        Ok(false)
    }
```
