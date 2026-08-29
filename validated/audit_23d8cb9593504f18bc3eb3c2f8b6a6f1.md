### Title
Deleted accounts leave orphaned PromiseYield/PostponedReceipt/ReceivedData trie entries because `account_storage_usage` never counts them and `remove_account` never cleans them up - ([File: core/store/src/utils/mod.rs -> remove_account])

### Summary
`action_delete_account` (`runtime/runtime/src/actions.rs:326-347`) gates deletion solely on `Account::storage_usage()`, a field that is never incremented for `ReceivedData`, `PostponedReceipt`/`PostponedReceiptId`/`PendingDataCount`, `PromiseYieldReceipt`, or `PromiseYieldStatus` trie entries. `remove_account` (`core/store/src/utils/mod.rs:505-575`) only deletes the `Account`, `ContractCode`, access keys/gas-key nonces, and `ContractData` — it never iterates or removes any of the above receipt-related keys for the account being deleted. An attacker can cheaply accumulate many such entries against their own account via `promise_yield_create`, then delete the account while `storage_usage` stays under `Account::MAX_ACCOUNT_DELETION_STORAGE_USAGE`, permanently orphaning that data in the trie.

### Finding Description
`action_delete_account` computes the deletable size as: [1](#0-0) 
This is purely a function of `account.storage_usage()`. That field is populated at genesis (and incrementally at runtime) explicitly excluding receipt-related records, as documented directly in the genesis storage accountant: [2](#0-1) 
`StateRecord::PostponedReceipt`, `ReceivedData`, and `DelayedReceipt` all map to `None` — "we don't charge storage for receipts." The same holds for the runtime path: `promise_yield_create` writes `PromiseYieldStatus` straight to the trie via `ext.rs::create_promise_yield_receipt`/`create_promise_yield_receipt_with_id`, and when the resulting receipt is delivered to the (self) receiver, `set_promise_yield_receipt` persists a `PromiseYieldReceipt{receiver_id, data_id}` entry — neither call touches `account.set_storage_usage(...)`: [3](#0-2) [4](#0-3) [5](#0-4) 

`remove_account` — the function actually invoked by `action_delete_account` to purge the account's trie footprint — only removes `Account`, `ContractCode`, access keys/gas-key nonces, and `ContractData` entries: [6](#0-5) 
It never iterates `PromiseYieldStatus`, `PromiseYieldReceipt`, `PostponedReceipt`/`PostponedReceiptId`, `PendingDataCount`, or `ReceivedData` keys scoped to the deleted `account_id`.

Attack flow: an ordinary funded account repeatedly calls `promise_yield_create`/`promise_yield_create_with_id` against itself (paying only `yield_create_base`/`yield_create_byte` gas fees; no storage-staking deposit is required for these entries since they never touch `storage_usage`). Each call writes a `PromiseYieldStatus` entry and, once the self-directed yield receipt is delivered, a `PromiseYieldReceipt` entry, both keyed by the account's `account_id`. None of this is reflected in `Account::storage_usage()`. The attacker then submits `DeleteAccount`; `action_delete_account` sees a small `account_storage_usage` (well under the 10,000-byte `MAX_ACCOUNT_DELETION_STORAGE_USAGE`), so deletion proceeds, `remove_account` runs, and the account record and its keys/contract data disappear — but the `PromiseYieldStatus`/`PromiseYieldReceipt` (and, via unresolved cross-contract calls, `PostponedReceipt`/`PostponedReceiptId`/`PendingDataCount`/`ReceivedData`) entries remain in the trie forever, keyed to an account that no longer exists and can never be cleaned up by any subsequent action.

### Impact Explanation
This is a state-bloat / permanent trie-freeze issue: bytes written to the trie are never charged to any account's storage balance and are never removed, so they accumulate indefinitely and grow the shard's state size and state-root proof size for unrelated operations on that shard. This matches the "freezing/bloat of chain state" bounty category (untracked, unbounded, permanently unclearable per-account trie residue), rather than direct fund theft.

### Likelihood Explanation
Fully reachable by an unprivileged, ordinary funded account with no special permissions: deploy any contract exposing `promise_yield_create`/`promise_yield_create_with_id`, call it N times (cost is only gas, no deposit is required since the entries never raise `storage_usage`), then submit a standard `DeleteAccount` action. Repeatable by any number of distinct accounts, at gas cost only (bounded by the `yield_create_base`/`yield_create_byte` fees), making this cheap and easily automatable.

### Recommendation
Either (a) make `remove_account` enumerate and delete all `PromiseYieldStatus`, `PromiseYieldReceipt`, `PostponedReceipt`/`PostponedReceiptId`, `PendingDataCount`, and `ReceivedData` trie entries scoped to `account_id` before allowing deletion, or (b) charge these entries against `Account::storage_usage()` when written (and credit it back when consumed/resolved) so that `action_delete_account`'s size check reflects the true trie footprint and rejects deletion (or requires cleanup) when such entries exist. Additionally, `action_delete_account` should probably refuse deletion outright while the account has any outstanding postponed/yielded receipts, mirroring the existing `DeleteAccountStaking` style precondition.

### Proof of Concept
1. Deploy a test contract exposing a method that calls `promise_yield_create_with_id` with a unique yield id and does not resume it.
2. From a funded test account, call this method N times (e.g., N = 1000), each with a distinct yield id, and let each yield receipt persist (never call `yield_resume`).
3. Assert via `view_account` that `storage_usage` stays roughly constant / near-baseline (well under `Account::MAX_ACCOUNT_DELETION_STORAGE_USAGE`), while directly inspecting the trie (e.g., via `state-viewer`/DB iteration over `TrieKey::PromiseYieldStatus`/`PromiseYieldReceipt` prefixed by the account id) shows N growing entries.
4. Submit `DeleteAccount` for the test account; assert it succeeds (`FinalExecutionStatus::SuccessValue`).
5. After deletion, re-scan the trie for `PromiseYieldStatus`/`PromiseYieldReceipt` keys under the now-deleted `account_id` prefix and assert they are still present (non-empty), proving orphaned, unbilled, unremovable trie bytes survive account deletion indefinitely.

### Citations

**File:** runtime/runtime/src/actions.rs (L325-347)
```rust
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
```

**File:** core/store/src/genesis/state_applier.rs (L44-76)
```rust
        let account_and_storage = match record {
            StateRecord::Account { account_id, .. } => {
                Some((account_id.clone(), self.config.num_bytes_account))
            }
            StateRecord::Data { account_id, data_key, value } => {
                let storage_usage =
                    self.config.num_extra_bytes_record + data_key.len() as u64 + value.len() as u64;
                Some((account_id.clone(), storage_usage))
            }
            StateRecord::Contract { account_id, code } => {
                Some((account_id.clone(), code.len() as u64))
            }
            StateRecord::AccessKey { account_id, public_key, access_key } => {
                let access_key: AccessKey = access_key.clone();
                let storage_usage = self.config.num_extra_bytes_record
                    + public_key.trie_id_len() as u64
                    + borsh::object_length(&access_key).unwrap() as u64;
                Some((account_id.clone(), storage_usage))
            }
            StateRecord::GasKeyNonce { account_id, public_key, index: _index, nonce } => {
                let storage_usage = self.config.num_extra_bytes_record
                    + public_key.trie_id_len() as u64
                    + size_of::<NonceIndex>() as u64
                    + borsh::object_length(&nonce).unwrap() as u64;
                Some((account_id.clone(), storage_usage))
            }
            StateRecord::PostponedReceipt(_) => None,
            StateRecord::ReceivedData { .. } => None,
            StateRecord::DelayedReceipt(_) => None,
        };
        if let Some((account_id, storage_usage)) = account_and_storage {
            *self.result.entry(account_id).or_default() += storage_usage;
        }
```

**File:** runtime/runtime/src/ext.rs (L353-369)
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
```

**File:** core/store/src/utils/mod.rs (L200-211)
```rust
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
```

**File:** core/store/src/utils/mod.rs (L499-575)
```rust
pub struct RemoveAccountResult {
    pub gas_key_nonce_count: usize,
    pub gas_key_nonce_total_key_bytes: usize, // used to calculate compute cost
}

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

**File:** runtime/runtime/src/lib.rs (L1495-1499)
```rust
            VersionedReceiptEnum::PromiseYield(_) => {
                // Received a new PromiseYield receipt. We simply store it and await
                // the corresponding PromiseResume receipt.
                set_promise_yield_receipt(state_update, receipt);
            }
```
