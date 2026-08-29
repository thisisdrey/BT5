### Title
`remove_account` fails to clear receipt/data-availability TrieKeys, leaving stale postponed self-receipts executable after account re-creation - (File: `core/store/src/utils/mod.rs`)

### Summary
`remove_account` in `core/store/src/utils/mod.rs` only deletes `TrieKey::Account`, `TrieKey::ContractCode`, all `TrieKey::AccessKey`/`TrieKey::GasKeyNonce` entries, and `TrieKey::ContractData` entries for the target account. It does **not** remove `TrieKey::ReceivedData`, `TrieKey::PostponedReceipt`, `TrieKey::PromiseYieldReceipt`, `TrieKey::PromiseYieldStatus`, `TrieKey::YieldIdToDataId`, or `TrieKey::DataIdToYieldId` keys scoped to that account, even though public setters for all of these exist in the same file.

### Finding Description
`remove_account` (`core/store/src/utils/mod.rs:505-575`) performs exactly three cleanup passes:
1. `state_update.remove(TrieKey::Account {..})` and `TrieKey::ContractCode {..}` [1](#0-0) 
2. Iterates `get_raw_prefix_for_access_keys` and removes every `AccessKey`/`GasKeyNonce` entry [2](#0-1) 
3. Iterates `get_raw_prefix_for_contract_data` and removes every `ContractData` entry [3](#0-2) 

It never touches keys produced by `set_received_data`, `set_postponed_receipt`, `set_promise_yield_receipt`, `set_promise_yield_status`, or `set_yield_id_mapping`, all defined in the same file. [4](#0-3) [5](#0-4) 

A table/differential test as described (set one value per variant for a fixed `account_id`, call `remove_account`, then check `trie` for leftovers under that account's prefix) would fail for these six variants: `ReceivedData`, `PostponedReceipt`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, `DataIdToYieldId`. (`PostponedReceiptId`/`PendingDataCount` column ids exist in `core/primitives/src/trie_key.rs`'s `col` module, but I found no corresponding public setter for them in `core/store/src/utils/mod.rs`, so they are out of scope for a test restricted to that file's setters.) [6](#0-5) 

An attacker fully controlling their own account/contract can legitimately create such entries before deleting the account: e.g. a promise combinator (`promise_and`/callback) can leave the account with an in-flight `PostponedReceipt` whose `predecessor_id == receiver_id == account_id` (a self-receipt) together with matching `ReceivedData`/pending-count bookkeeping, embedding privileged actions (e.g. `AddKey`, `DeleteKey`, `Stake`, `DeployContract`) that pass the runtime's "self receipt" trust checks in `runtime/runtime/src/actions.rs`/`action_validation.rs` purely because `predecessor_id == receiver_id`, independent of which keys currently exist on the account.

### Impact Explanation
This is a genuine data-hygiene defect: `remove_account`'s "set of keys written" (across the trie-key surface reachable from ordinary transactions) is not equal to its "set of keys cleared." However, I was not able to fully trace, within the available tool budget, the precise mechanics of `check_actor_permissions` and the postponed-receipt/promise-yield redelivery path in `runtime/runtime/src/actions.rs` and `runtime/runtime/src/action_validation.rs` to confirm a cross-owner escalation. The main structural obstacle to the "authorization escalation across accounts" framing is that non-implicit account re-creation is namespace-gated (a sub-account can only be re-created by its same parent account; implicit accounts are created only by a matching-key-signed transfer, not by an arbitrary `CreateAccount` action), which limits the scenario largely to an attacker re-creating their *own* account — in which case they already control the resulting state and gain no privilege they did not already have. A cross-owner escalation would require a narrower scenario (e.g. top-level-account name release/re-registration by an unrelated party), which I could not confirm is reachable purely from an "unprivileged sender" as scoped by this audit.

### Likelihood Explanation
Confirmed: the code-level gap in `remove_account` exists and is reproducible today. Unconfirmed/uncertain: whether it is actually exploitable end-to-end for cross-account/cross-owner authorization escalation given account re-creation namespace restrictions, since I could not complete tracing `check_actor_permissions` and the delayed-receipt redelivery path.

### Recommendation
`remove_account` should also enumerate and remove, scoped to `account_id`: `ReceivedData`, `PostponedReceipt` (and any `PostponedReceiptId`/`PendingDataCount` bookkeeping if it exists), `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, and `DataIdToYieldId` entries, mirroring the existing access-key and contract-data iteration/removal passes, so that deleting an account is guaranteed to leave no residual receipt-processing state behind.

### Proof of Concept
Table test in `core/store/src/utils/mod.rs` (or a new test module): for each variant in `{ReceivedData, PostponedReceipt, PromiseYieldReceipt, PromiseYieldStatus, YieldIdToDataId, DataIdToYieldId}`, construct a `TrieUpdate`, call the corresponding setter (`set_received_data`, `set_postponed_receipt`, `set_promise_yield_receipt`, `set_promise_yield_status`, `set_yield_id_mapping`) for a fixed `account_id`, call `remove_account(account_id)`, commit, and assert via `trie_key_parsers`-derived prefixes that no matching key remains. Expected result based on code reading: the assertion fails (key still present) for all six variants listed above, and passes for `Account`, `ContractCode`, `AccessKey`, `GasKeyNonce`, `ContractData`.

### Citations

**File:** core/store/src/utils/mod.rs (L76-127)
```rust
pub fn set_received_data(
    state_update: &mut TrieUpdate,
    receiver_id: AccountId,
    data_id: CryptoHash,
    data: &ReceivedData,
) {
    set(state_update, TrieKey::ReceivedData { receiver_id, data_id }, data);
}

pub fn get_received_data(
    trie: &dyn TrieAccess,
    receiver_id: &AccountId,
    data_id: CryptoHash,
) -> Result<Option<ReceivedData>, StorageError> {
    get(trie, &TrieKey::ReceivedData { receiver_id: receiver_id.clone(), data_id })
}

/// Returns the size (in bytes) of the trie value holding the `ReceivedData`
/// for `data_id`, without loading — and therefore without recording into the
/// state witness — the value itself. Returns `None` if there is no such entry.
pub fn get_received_data_size(
    state_update: &TrieUpdate,
    receiver_id: &AccountId,
    data_id: CryptoHash,
) -> Result<Option<u32>, StorageError> {
    let value_ptr = state_update.get_ref(
        &TrieKey::ReceivedData { receiver_id: receiver_id.clone(), data_id },
        KeyLookupMode::MemOrFlatOrTrie,
        AccessOptions::DEFAULT,
    )?;
    Ok(value_ptr.map(|ptr| ptr.len()))
}

pub fn has_received_data(
    trie: &dyn TrieAccess,
    receiver_id: &AccountId,
    data_id: CryptoHash,
) -> Result<bool, StorageError> {
    trie.contains_key(
        &TrieKey::ReceivedData { receiver_id: receiver_id.clone(), data_id },
        AccessOptions::DEFAULT,
    )
}

pub fn set_postponed_receipt(state_update: &mut TrieUpdate, receipt: &Receipt) {
    assert!(matches!(receipt.versioned_receipt(), VersionedReceiptEnum::Action(_)));
    let key = TrieKey::PostponedReceipt {
        receiver_id: receipt.receiver_id().clone(),
        receipt_id: *receipt.receipt_id(),
    };
    set(state_update, key, receipt);
}
```

**File:** core/store/src/utils/mod.rs (L200-297)
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
}

pub fn remove_promise_yield_receipt(
    state_update: &mut TrieUpdate,
    receiver_id: &AccountId,
    data_id: CryptoHash,
) {
    state_update.remove(TrieKey::PromiseYieldReceipt { receiver_id: receiver_id.clone(), data_id });
}

pub fn get_promise_yield_receipt(
    trie: &dyn TrieAccess,
    receiver_id: &AccountId,
    data_id: CryptoHash,
) -> Result<Option<Receipt>, StorageError> {
    get(trie, &TrieKey::PromiseYieldReceipt { receiver_id: receiver_id.clone(), data_id })
}

pub fn has_promise_yield_receipt(
    trie: &dyn TrieAccess,
    receiver_id: AccountId,
    data_id: CryptoHash,
) -> Result<bool, StorageError> {
    trie.contains_key(
        &TrieKey::PromiseYieldReceipt { receiver_id, data_id },
        AccessOptions::DEFAULT,
    )
}

pub fn get_promise_yield_status(
    trie: &dyn TrieAccess,
    receiver_id: &AccountId,
    data_id: CryptoHash,
) -> Result<Option<PromiseYieldStatus>, StorageError> {
    get(trie, &TrieKey::PromiseYieldStatus { receiver_id: receiver_id.clone(), data_id })
}

pub fn has_promise_yield_status(
    trie: &dyn TrieAccess,
    receiver_id: &AccountId,
    data_id: CryptoHash,
) -> Result<bool, StorageError> {
    trie.contains_key(
        &TrieKey::PromiseYieldStatus { receiver_id: receiver_id.clone(), data_id },
        AccessOptions::DEFAULT,
    )
}

pub fn set_promise_yield_status(
    state_update: &mut TrieUpdate,
    receiver_id: &AccountId,
    data_id: CryptoHash,
    status: PromiseYieldStatus,
) {
    set(
        state_update,
        TrieKey::PromiseYieldStatus { receiver_id: receiver_id.clone(), data_id },
        &status,
    );
}

pub fn remove_promise_yield_status(
    state_update: &mut TrieUpdate,
    receiver_id: &AccountId,
    data_id: CryptoHash,
) {
    state_update.remove(TrieKey::PromiseYieldStatus { receiver_id: receiver_id.clone(), data_id });
}

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
```

**File:** core/store/src/utils/mod.rs (L509-510)
```rust
    state_update.remove(TrieKey::Account { account_id: account_id.clone() });
    state_update.remove(TrieKey::ContractCode { account_id: account_id.clone() });
```

**File:** core/store/src/utils/mod.rs (L516-553)
```rust
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
```

**File:** core/store/src/utils/mod.rs (L556-573)
```rust
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
```

**File:** core/primitives/src/trie_key.rs (L89-102)
```rust
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
