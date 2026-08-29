### Title
`remove_account` fails to purge `YieldIdToDataId`/`DataIdToYieldId` (and `PromiseYieldReceipt`/`PromiseYieldStatus`) rows, letting a re-created account inherit a dangling yielded-promise mapping - ([File: core/store/src/utils/mod.rs::remove_account])

### Summary
`remove_account` in `core/store/src/utils/mod.rs` explicitly removes `Account`, `ContractCode`, `AccessKey`/`GasKeyNonce`, and `ContractData` rows for a deleted account, but never touches `TrieKey::YieldIdToDataId`, `TrieKey::DataIdToYieldId`, `TrieKey::PromiseYieldReceipt`, or `TrieKey::PromiseYieldStatus` rows keyed by that account's `receiver_id`. `action_delete_account` in `runtime/runtime/src/actions.rs` only validates storage-usage size and staking/gas-key balances before calling `remove_account`, with no check for pending yielded promises, so an account with an in-flight `promise_yield_create`/`promise_yield_create_with_id` can be deleted while these rows remain in the trie.

### Finding Description
An unprivileged attacker deploys a contract to a sub-account (e.g. `victim.attacker.near`), calls `promise_yield_create_with_id`, which writes `TrieKey::YieldIdToDataId{receiver_id, yield_id}`, `TrieKey::DataIdToYieldId{receiver_id, data_id}`, `TrieKey::PromiseYieldReceipt{receiver_id, data_id}`, and `TrieKey::PromiseYieldStatus{receiver_id, data_id}` all keyed by the attacker's own `receiver_id` [1](#0-0) . The attacker then submits `DeleteAccountAction` as the final action. `action_delete_account` checks only `MAX_ACCOUNT_DELETION_STORAGE_USAGE` and `GasKeyInfo::MAX_BALANCE_TO_BURN`, with no check for pending yields, and calls `remove_account` [2](#0-1) .

`remove_account` only clears `Account`, `ContractCode`, access-key/gas-key rows, and `ContractData`; it never iterates or removes the promise-yield-related columns for the account [3](#0-2) . This is despite these four columns being explicitly documented as "columns with account id in key" alongside `Account`/`ContractData` [4](#0-3) .

Since a sub-account name can be freely reused after deletion (any third party can `CreateAccount` + `AddKey` under the same `account_id`), the new account inherits the stale rows. When the new account (or the pending `PromiseYieldTimeout` queue entry, which is a global, per-shard structure independent of the account) triggers resolution of that `data_id`/`yield_id`, the runtime performs lookups scoped only by `receiver_id` (the account name string), not by any generation/incarnation marker: `get_data_id_for_yield_id`/`has_yield_id_mapping` [5](#0-4)  and `has_promise_yield_receipt`/`has_promise_yield_status` used by `submit_promise_resume_data` [6](#0-5) . Because the old `PromiseYieldReceipt` row is still present, `submit_promise_resume_data` returns `true` and the runtime resumes and eventually executes the **prior incarnation's stored receipt** (`get_promise_yield_receipt` / `apply_action_receipt`) under the **new** account's execution context, as shown in the resume-processing logic [7](#0-6) . This violates authorization exactness: state written by the previous account incarnation is executed against/attributable to the new incarnation.

### Impact Explanation
This is authorization escalation across account incarnations/promises: a re-created account can resolve, forge, or have executed against it a promise/receipt that belongs to the deleted prior owner of the account name. Depending on what actions were queued in the stale `PromiseYieldReceipt` (e.g., FunctionCall with an attached deposit that was already deducted from the balance at receipt-creation time), this can result in unexpected execution or fund movement not authorized by the new account holder, and in state that diverges from what a correct trie should contain post-deletion (dangling rows never cleaned, contradicting `remove_account`'s stated purpose of removing "account, code and all access keys ... associated to it").

### Likelihood Explanation
- Precondition: attacker's account must have no locked stake (trivial - unprivileged sub-accounts are never validators) and storage usage under `MAX_ACCOUNT_DELETION_STORAGE_USAGE` (trivially satisfiable).
- Cost: one contract deployment, one `promise_yield_create_with_id` call, one `DeleteAccountAction` - all ordinary, low-cost transactions available to any unprivileged account.
- Repeatable: nothing in `action_delete_account` or `remove_account` prevents this pattern from being repeated for any sub-account name the attacker controls.
- The only missing piece confirmed but not runtime-tested here is the exact economic magnitude of consequence (whether attached deposits in the stale receipt can be captured/lost); this depends on further receipt-content-specific tracing not fully completed in this review.

### Recommendation
In `remove_account` (`core/store/src/utils/mod.rs`), before or alongside removing `Account`/`ContractCode`/access-keys/`ContractData`, also iterate and remove all `TrieKey::YieldIdToDataId`, `TrieKey::DataIdToYieldId`, `TrieKey::PromiseYieldReceipt`, and `TrieKey::PromiseYieldStatus` rows keyed by `account_id` as `receiver_id`, mirroring the existing prefix-iteration pattern used for `ContractData`. Alternatively/additionally, reject `DeleteAccountAction` (return a new `ActionErrorKind`, e.g. `DeleteAccountWithPendingYield`) when any pending yield state exists for the account, similar to the existing `DeleteAccountStaking` check.

### Proof of Concept
Integration/runtime-test-loop plan:
1. Deploy a contract to `sub.alice.near` and call `promise_yield_create_with_id` with a fixed `yield_id`, confirming `get_data_id_for_yield_id`/`get_yield_id_for_data_id` return `Some(..)` and `has_promise_yield_receipt` is `true`.
2. Submit `DeleteAccountAction` for `sub.alice.near` (beneficiary `alice.near`) in the same or a following block; assert it succeeds (`SuccessValue`) and `view_account("sub.alice.near")` fails.
3. Assert (this should fail today, demonstrating the bug): `get_data_id_for_yield_id`/`get_yield_id_for_data_id`/`has_promise_yield_receipt`/`has_promise_yield_status` for `sub.alice.near` still return `Some`/`true` post-deletion instead of `None`/`false`.
4. Re-create `sub.alice.near` via `CreateAccount` + `AddKey` signed by `alice.near`, deploy a fresh (different) contract.
5. From the new contract, call `promise_yield_resume_with_yield_id` with the original `yield_id` (or let the original `PromiseYieldTimeout` queue entry fire), and assert that the stale `PromiseYieldReceipt` from the deleted incarnation is executed/resolved against the new account, confirming cross-incarnation state leakage (`AUTHORIZATION_EXACTNESS` violated).

### Citations

**File:** runtime/runtime/src/ext.rs (L371-400)
```rust
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
```

**File:** runtime/runtime/src/ext.rs (L402-426)
```rust
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

**File:** core/store/src/utils/mod.rs (L299-324)
```rust
pub fn get_data_id_for_yield_id(
    trie: &dyn TrieAccess,
    receiver_id: &AccountId,
    yield_id: YieldId,
) -> Result<Option<CryptoHash>, StorageError> {
    get(trie, &TrieKey::YieldIdToDataId { receiver_id: receiver_id.clone(), yield_id })
}

pub fn get_yield_id_for_data_id(
    trie: &dyn TrieAccess,
    receiver_id: &AccountId,
    data_id: CryptoHash,
) -> Result<Option<YieldId>, StorageError> {
    get(trie, &TrieKey::DataIdToYieldId { receiver_id: receiver_id.clone(), data_id })
}

pub fn has_yield_id_mapping(
    trie: &dyn TrieAccess,
    receiver_id: &AccountId,
    yield_id: YieldId,
) -> Result<bool, StorageError> {
    trie.contains_key(
        &TrieKey::YieldIdToDataId { receiver_id: receiver_id.clone(), yield_id },
        AccessOptions::DEFAULT,
    )
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
