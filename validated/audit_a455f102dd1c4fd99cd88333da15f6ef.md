### Title
Stale `YieldIdToDataId`/`DataIdToYieldId` trie entries survive `DeleteAccount`, permanently blocking `promise_yield_create_with_id` for a reused account name - (`core/store/src/utils/mod.rs`, `runtime/runtime/src/actions.rs`)

### Summary
`remove_account` (`core/store/src/utils/mod.rs:505-575`) only deletes `TrieKey::Account`, `TrieKey::ContractCode`, `TrieKey::AccessKey`/`GasKeyNonce`, and `TrieKey::ContractData` entries. It never removes `TrieKey::YieldIdToDataId`, `TrieKey::DataIdToYieldId`, or `TrieKey::PromiseYieldStatus` entries created by `create_promise_yield_receipt_with_id` (`runtime/runtime/src/ext.rs:371-400`). An attacker who controls account `A` can create a yield with a chosen `yield_id`, self-delete `A` via `DeleteAccount`, and leave the mapping permanently orphaned in the trie under the (now-deletable and reusable) account name.

### Finding Description
`promise_yield_create_with_id` (`runtime/near-vm-runner/src/logic/logic.rs:3727-3813`) calls `create_promise_yield_receipt_with_id`, which writes bidirectional mappings via `set_yield_id_mapping` (`core/store/src/utils/mod.rs:281-297`):
- `TrieKey::YieldIdToDataId{receiver_id: A, yield_id}` → `data_id`
- `TrieKey::DataIdToYieldId{receiver_id: A, data_id}` → `yield_id`
- `TrieKey::PromiseYieldStatus{receiver_id: A, data_id}` → `Yielded`

These entries are only cleaned up when the corresponding `PromiseResume` receipt is delivered (`runtime/runtime/src/lib.rs:1523-1537`, calling `remove_yield_id_mappings`) — i.e., cleanup is tied to *resuming the yield*, not to the account's lifecycle.

`action_delete_account` (`runtime/runtime/src/actions.rs:314-390`) calls `remove_account` (`core/store/src/utils/mod.rs:505-575`), which is documented as "Removes account, code and all access keys and gas keys" and only iterates/removes `Account`, `ContractCode`, `AccessKey`/`GasKeyNonce`, and `ContractData` keys. It has no code path touching `YieldIdToDataId`, `DataIdToYieldId`, `PromiseYieldStatus`, or `PromiseYieldReceipt` trie keys.

Attack flow:
1. Attacker controls account `A` (owns a full-access key). Calls a contract action sequence containing `promise_yield_create_with_id` with an attacker-chosen `yield_id`, writing the mapping under `receiver_id: A`.
2. Attacker submits `DeleteAccount{beneficiary_id}` for `A`. `action_delete_account` checks only `account_storage_usage > Account::MAX_ACCOUNT_DELETION_STORAGE_USAGE` (based on `account.storage_usage()`); nothing in this check inspects yield-mapping state, so a small yield entry does not block deletion. `remove_account` deletes the `Account` record but leaves the `YieldIdToDataId`/`DataIdToYieldId` entries behind, permanently orphaned in the trie (unreachable and unaccounted, since the account record that "owned" that storage usage is gone).
3. A new owner recreates account `A` (NEAR permits recreating a previously-deleted account id) and, independently, calls `promise_yield_create_with_id` with the same `yield_id` (this only collides if the new owner happens to pick the same 32-byte ID as the attacker predicted/controlled — a real but narrow precondition; more importantly, the vulnerability's core defect is unconditional: **the leaked trie rows exist for that receiver_id/yield_id/data_id forever, regardless of whether a collision is exploited**).
4. If the new owner's yield_id does collide with the stale entry, `create_promise_yield_receipt_with_id` calls `has_yield_id_mapping` (`runtime/runtime/src/ext.rs:378-382`), finds the stale row still present, and returns `Ok(None)` — `promise_yield_create_with_id` then returns the `u64::MAX` sentinel to the new owner's contract, silently refusing to create the promise the new owner asked for. No panic occurs, and no resolution against the stale `data_id` happens (the duplicate check simply rejects the create outright, since it doesn't check whether the "existing" mapping's underlying receipt/account context still corresponds to a live account).

This is a resource-hygiene / storage-leak defect (`DeleteAccount` does not fully clean an account's yield-related trie subtree) rather than a state-corruption or authorization bug: the surviving mapping does not get *resolved* to the wrong receipt for the new owner (the check short-circuits before touching the new owner's receipt/data at all), and it does not cause a shard-halting panic in the traced apply path.

### Impact Explanation
The scoped impact matches "permanently stuck promise on victim account" only in the narrow sense that a specific `(account_name, yield_id)` pair becomes permanently unusable via `promise_yield_create_with_id` after the account is deleted and recreated, for as long as the new account never resumes/rewrites that exact ID (it never can, since the mapping is orphaned and no receipt exists to resume it). This is a liveness annoyance limited to a single 32-byte ID choice on a specific account name; it does not corrupt other IDs, does not affect account funds, does not cause consensus divergence or a shard-halting panic, and is trivially avoidable by the new owner using a different `yield_id` (the ID space is 2^256). For a real attacker to weaponize this against a *specific future victim*, the attacker would additionally need the victim's contract to deterministically choose the exact same `yield_id` after re-registering the same account name — a scenario not demonstrated as reachable from the traced code paths.

### Likelihood Explanation
Reproducing the storage leak itself is trivial and 100% reliable (self-own account, create-with-id, self-delete). However, causing an actual "corrupted promise flow" for an unrelated new owner requires a yield_id collision that the attacker cannot force absent a specific victim contract design that deterministically re-derives the same ID after re-registering the account name — this is a low-likelihood, contrived precondition rather than a generally reachable exploit against arbitrary victims.

### Recommendation
Extend `remove_account` (`core/store/src/utils/mod.rs`) to also iterate and remove `TrieKey::PromiseYieldStatus`, `TrieKey::YieldIdToDataId`, `TrieKey::DataIdToYieldId`, and `TrieKey::PromiseYieldReceipt` entries scoped to `account_id` before/while deleting the account (mirroring the existing access-key/contract-data iteration-and-removal pattern), so no yield-related state survives account deletion. Alternatively/additionally, account for outstanding yield state in the `MAX_ACCOUNT_DELETION_STORAGE_USAGE`/self-delete eligibility check so accounts with pending yields cannot self-delete until they resume or time out.

### Proof of Concept
Unit test plan (extending `integration-tests/src/tests/runtime/test_yield_resume.rs` patterns):
1. Deploy `test_contract.alice.near` (or similar) and call `promise_yield_create_with_id` with a fixed `yield_id = [9u8; 32]`, asserting success and capturing that `TrieKey::YieldIdToDataId{receiver_id, yield_id}` exists (via a direct trie read helper `get_data_id_for_yield_id`).
2. Submit a `DeleteAccount` action for that contract account with an arbitrary beneficiary; assert success.
3. Directly query the trie for `TrieKey::YieldIdToDataId{receiver_id, yield_id}` and `TrieKey::DataIdToYieldId{receiver_id, data_id}` and assert they are **still present** despite `TrieKey::Account{account_id}` being gone — this demonstrates the storage leak caused by `remove_account`'s omission.
4. Recreate the account (`CreateAccount` + redeploy contract) and call `promise_yield_create_with_id` again with the **same** `yield_id`; assert the call returns the `u64::MAX` sentinel (i.e., `has_yield_id_mapping` incorrectly reports a collision against a receipt tied to the deleted account), proving the new owner cannot use that ID even though no legitimate pending yield exists for the new account. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** core/store/src/utils/mod.rs (L281-333)
```rust
pub fn set_yield_id_mapping(
    state_update: &mut TrieUpdate,
    receiver_id: &AccountId,
    yield_id: YieldId,
    data_id: CryptoHash,
) {
    set(
        state_update,
        TrieKey::YieldIdToDataId { receiver_id: receiver_id.clone(), yield_id },
        &data_id,
    );
    set(
        state_update,
        TrieKey::DataIdToYieldId { receiver_id: receiver_id.clone(), data_id },
        &yield_id,
    );
}

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

pub fn remove_yield_id_mappings(
    state_update: &mut TrieUpdate,
    receiver_id: &AccountId,
    yield_id: YieldId,
    data_id: CryptoHash,
) {
    state_update.remove(TrieKey::YieldIdToDataId { receiver_id: receiver_id.clone(), yield_id });
    state_update.remove(TrieKey::DataIdToYieldId { receiver_id: receiver_id.clone(), data_id });
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

**File:** runtime/runtime/src/lib.rs (L1523-1537)
```rust
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
```
