### Title
Incomplete per-account trie cleanup in `remove_account` leaves stale `ReceivedData`/`PostponedReceipt`/`PendingDataCount`/`PromiseYield*` rows after account deletion, enabling fund/execution misdirection to a re-created account - (File: `core/store/src/utils/mod.rs`)

### Summary
`remove_account` only purges `TrieKey::Account`, `TrieKey::ContractCode`, `TrieKey::AccessKey`/gas-key rows (via `get_raw_prefix_for_access_keys`), and `TrieKey::ContractData` (via `get_raw_prefix_for_contract_data`). It never iterates or removes rows under `col::RECEIVED_DATA`, `col::POSTPONED_RECEIPT_ID`/`PENDING_DATA_COUNT`, `col::POSTPONED_RECEIPT`, `col::PROMISE_YIELD_RECEIPT`, `col::PROMISE_YIELD_STATUS`, `col::YIELD_ID_TO_DATA_ID`, or `col::DATA_ID_TO_YIELD_ID`, even though these are all keyed with the account id and logically belong to the account's lifecycle.

### Finding Description
`remove_account` in [1](#0-0)  removes exactly four categories of state:
- `TrieKey::Account` / `TrieKey::ContractCode` (direct removes)
- access keys / gas key nonces via prefix iteration using `trie_key_parsers::get_raw_prefix_for_access_keys` [2](#0-1) 
- contract storage via prefix iteration using `trie_key_parsers::get_raw_prefix_for_contract_data` [3](#0-2) 

There is no equivalent prefix-iteration/removal step for `ReceivedData`, `PostponedReceipt`, `PendingDataCount`/`PostponedReceiptId`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, or `DataIdToYieldId`, all of which the question states are enumerated in `col::COLUMNS_WITH_ACCOUNT_ID_IN_KEY`. Helper setters/removers for these keys exist elsewhere in the same file (`set_received_data`, `set_postponed_receipt`/`remove_postponed_receipt`, `set_promise_yield_receipt`/`remove_promise_yield_receipt`, `set_promise_yield_status`/`remove_promise_yield_status`, `set_yield_id_mapping`/`remove_yield_id_mappings`) [4](#0-3) , but `remove_account` calls none of them, confirming the asymmetry is a real gap rather than dead code.

Attack surface: an attacker's own account `A` accumulates cross-shard, data-dependent action receipts (e.g. via `FunctionCall` promises that await `PromiseResult`/yield-resume), which are persisted as `PostponedReceipt`/`PendingDataCount` (and, for early-arriving data, `ReceivedData`) rows keyed to `A` while awaiting matching input data that has not yet arrived at `A`'s shard. The attacker then submits `DeleteAccount(A)` followed by `CreateAccount(A)` (both ordinary, self-signed actions requiring only a full-access key on `A`, no special privilege). `remove_account`'s partial cleanup leaves the pending obligation rows intact under the same account-id key prefix. When the awaited data (which the protocol guarantees exactly-once delivery for, independent of the attacker's control) later arrives at `A`, the runtime's data-resolution path looks up `PostponedReceipt`/`PendingDataCount` purely by account id and receipt id — it has no way to know the account was deleted and recreated in between — and will execute the stale postponed action receipt against whatever contract code the attacker has since deployed on the recreated `A`.

### Impact Explanation
Because a postponed `ActionReceipt` embeds actions (e.g. `Transfer`, `FunctionCall` with attached deposit) that were queued by a third party expecting them to execute against `A`'s pre-deletion contract logic, but the balance/action application only happens at receipt-resolution time (not at postponement time), the attacker can redeploy arbitrary code to `A` in between and have the stale receipt's attached deposit/actions applied to the new, attacker-controlled logic. This is an authorization/execution-context escalation across the account's own lifecycle boundary — funds or execution rights intended for the old occupant of `A` get delivered to code the attacker fully controls post-recreation. It matches NEAR's "authorization escalation across accounts or promises" bounty category, and depending on how the resolution code handles an unexpectedly-missing/mismatched `PendingDataCount` state, a similarly stale reference could instead trigger `StorageInconsistentState`-style panics (a shard-halting condition) rather than silent misapplication.

### Likelihood Explanation
Preconditions are cheap and fully within an ordinary account holder's capability: normal contract usage generating cross-shard/data-dependent postponed receipts, followed by self-submitted `DeleteAccount` + `CreateAccount` on the same account, which requires no special access beyond the account's own full-access key. I was not able to verify from the available index whether `DeleteAccount` action validation already blocks deletion when there are unresolved postponed receipts/pending data counts for the account, or the exact downstream behavior of the data-delivery/receipt-resolution code path in `runtime.rs` when it encounters these rows after a recreate — both would materially affect exploitability and severity, and I could not confirm them within the available tool budget. This uncertainty should be resolved before treating this as a confirmed, fully-weaponizable bug.

### Recommendation
Extend `remove_account` to also prefix-iterate and remove rows for `ReceivedData`, `PostponedReceipt`, `PendingDataCount`/`PostponedReceiptId`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, and `DataIdToYieldId` for the target account, mirroring the pattern already used for access keys and contract data, ideally by introducing prefix-helper functions analogous to `get_raw_prefix_for_access_keys`/`get_raw_prefix_for_contract_data` for each of these columns and driving the removal from the authoritative `col::COLUMNS_WITH_ACCOUNT_ID_IN_KEY` list to prevent future drift.

### Proof of Concept
Table-driven unit test in `core/store/src/utils/mod.rs` (or a `runtime`-level integration test):
1. For each column in `col::COLUMNS_WITH_ACCOUNT_ID_IN_KEY`, write one representative row for account `A` (e.g. `set_received_data`, `set_postponed_receipt`, `set_promise_yield_receipt`, `set_promise_yield_status`, `set_yield_id_mapping`, plus a `PendingDataCount`/`PostponedReceiptId` row and an `Account`/`AccessKey`/`ContractData` row for baseline).
2. Call `remove_account(&mut state_update, &A)`.
3. Re-iterate each column's account-scoped prefix and assert zero remaining rows.
Expected (current) result: `AccessKey`, `ContractData` columns are empty; `ReceivedData`, `PostponedReceipt`, `PendingDataCount`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, `DataIdToYieldId` columns still contain the previously written rows, demonstrating the incomplete cleanup.

### Citations

**File:** core/store/src/utils/mod.rs (L76-334)
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

pub fn remove_postponed_receipt(
    state_update: &mut TrieUpdate,
    receiver_id: &AccountId,
    receipt_id: CryptoHash,
) {
    state_update.remove(TrieKey::PostponedReceipt { receiver_id: receiver_id.clone(), receipt_id });
}

pub fn get_postponed_receipt(
    trie: &dyn TrieAccess,
    receiver_id: &AccountId,
    receipt_id: CryptoHash,
) -> Result<Option<Receipt>, StorageError> {
    get(trie, &TrieKey::PostponedReceipt { receiver_id: receiver_id.clone(), receipt_id })
}

pub fn get_delayed_receipt_indices(
    trie: &dyn TrieAccess,
) -> Result<DelayedReceiptIndices, StorageError> {
    Ok(get(trie, &TrieKey::DelayedReceiptIndices)?.unwrap_or_default())
}

// Adds the given receipt into the end of the delayed receipt queue in the state.
pub fn set_delayed_receipt(
    state_update: &mut TrieUpdate,
    delayed_receipts_indices: &mut DelayedReceiptIndices,
    receipt: &Receipt,
) {
    set(
        state_update,
        TrieKey::DelayedReceipt { index: delayed_receipts_indices.next_available_index },
        receipt,
    );
    delayed_receipts_indices.next_available_index = delayed_receipts_indices
        .next_available_index
        .checked_add(1)
        .expect("Next available index for delayed receipt exceeded the integer limit");
}

pub fn get_promise_yield_indices(
    trie: &dyn TrieAccess,
) -> Result<PromiseYieldIndices, StorageError> {
    Ok(get(trie, &TrieKey::PromiseYieldIndices)?.unwrap_or_default())
}

pub fn set_promise_yield_indices(
    state_update: &mut TrieUpdate,
    promise_yield_indices: &PromiseYieldIndices,
) {
    set(state_update, TrieKey::PromiseYieldIndices, promise_yield_indices);
}

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
}
```

**File:** core/store/src/utils/mod.rs (L505-575)
```rust
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

**File:** core/primitives/src/trie_key.rs (L897-903)
```rust
    pub fn get_raw_prefix_for_access_keys(account_id: &AccountId) -> Vec<u8> {
        let mut res = Vec::with_capacity(col::ACCESS_KEY.len() * 2 + account_id.len());
        res.push(col::ACCESS_KEY);
        res.extend(account_id.as_bytes());
        res.push(col::ACCESS_KEY);
        res
    }
```

**File:** core/primitives/src/trie_key.rs (L905-917)
```rust
    pub fn get_raw_prefix_for_contract_data(account_id: &AccountId, prefix: &[u8]) -> Vec<u8> {
        let mut res = Vec::with_capacity(
            col::CONTRACT_DATA.len()
                + account_id.len()
                + ACCOUNT_DATA_SEPARATOR.len()
                + prefix.len(),
        );
        res.push(col::CONTRACT_DATA);
        res.extend(account_id.as_bytes());
        res.push(ACCOUNT_DATA_SEPARATOR);
        res.extend(prefix);
        res
    }
```
