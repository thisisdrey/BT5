### Title
Stale `PostponedReceipt` entries survive `remove_account`, enabling privileged action replay against a recreated account - (File: core/store/src/utils/mod.rs)

### Summary
`remove_account` in `core/store/src/utils/mod.rs` deletes `TrieKey::Account`, `TrieKey::ContractCode`, all `TrieKey::AccessKey`/`TrieKey::GasKeyNonce` entries (via the access-key prefix iterator), and all `TrieKey::ContractData` entries, but never removes `TrieKey::PostponedReceipt{receiver_id, receipt_id}` entries belonging to the account being deleted. If an account is deleted while it still has an outstanding postponed receipt awaiting a `DataReceipt`, that receipt remains in trie state and can later be delivered and executed against whatever account subsequently occupies the same `account_id`.

### Finding Description
`remove_account` (core/store/src/utils/mod.rs, lines 505-575) performs targeted cleanup of exactly four state categories tied to an account: the account record itself, its contract code, its access keys/gas-key nonces (found via `trie_key_parsers::get_raw_prefix_for_access_keys`), and its contract data (via `trie_key_parsers::get_raw_prefix_for_contract_data`). There is no corresponding prefix iteration or removal for `TrieKey::PostponedReceipt` entries, even though `set_postponed_receipt`/`get_postponed_receipt`/`remove_postponed_receipt` (same file, lines 120-143) key postponed action receipts by `(receiver_id, receipt_id)`, i.e. tied to the account. [1](#0-0) 

Because `DeleteAccount` removes the account state via `remove_account`, but a postponed receipt is a separate, independently-keyed trie entry, it is not cleaned up as part of account deletion. When a `DataReceipt` matching the receipt's outstanding `input_data_id` is later delivered, the runtime's receipt-processing path (`runtime/runtime/src/lib.rs`, functions handling `apply_data_receipt`/postponed-receipt lookup via `get_postponed_receipt`/`remove_postponed_receipt`) will find the stale postponed receipt still present under `(receiver_id, receipt_id)` and proceed to execute its actions against whatever account now exists at that `account_id` — which, per the scenario, may be a completely different, newly-created and funded account controlled by someone else. [2](#0-1) 

I was able to confirm the concrete root cause (the missing `PostponedReceipt` cleanup in `remove_account`) directly in the code. I was **not** able to fully verify, within the tool budget available, two additional links in the claimed exploit chain from the repository itself:
1. Whether `action_delete_account` in `runtime/runtime/src/actions.rs` contains any guard that blocks `DeleteAccount` while postponed receipts targeting the account are outstanding (I located the function but did not get to read its body before the session ended).
2. The exact mechanics of `check_actor_permissions` for `AddKey` in `runtime/runtime/src/actions.rs` — in NEAR's actor model, actions such as `AddKey`/`DeleteKey`/`DeployContract` are authorized by requiring `predecessor_id == receiver_id` on the *receipt*, which for a "self" promise (an account issuing a promise back to itself, e.g., via a cross-contract callback with a data dependency) is trivially true by construction — this is not a new bypass introduced by the bug, but the described exploit's step 1 ("attacker deploys no contract, just crafts a manual DataReceipt dependency") is inconsistent with how `input_data_id` dependencies are actually created in NEAR: they arise only from a WASM contract's promise-batch/`.then()` API, not from a bare transaction. This detail in the question's proof idea does not match protocol capabilities and I could not verify a contract-free path to create such a postponed self-receipt.

### Impact Explanation
If the missing-cleanup issue is combined with a viable way to produce a self-targeted postponed receipt containing an owner-privileged action (e.g. `AddKey{FullAccessKey}`), the impact would be authorization escalation: an action authorized by the account's old occupant executes with full owner privilege against the account's new occupant after account deletion and recreation, matching NEAR's "authorization/consensus" bounty category (unauthorized full-access key injection into a funded account). This is a scoped, real impact category if the chain is completable.

### Likelihood Explanation
The concrete, verified defect (stale `PostponedReceipt` entries surviving `remove_account`) is real and requires no special privilege beyond normal account ownership and the ability to delete one's own account while a postponed receipt is outstanding — assuming nothing else in the runtime blocks `DeleteAccount` in that state (unverified here). However, the specific exploit narrative's step 1 (creating an attacker-controlled postponed self-receipt "without deploying a contract") is not consistent with how input-data dependencies are created in NEAR (they require a deployed contract using the promise/`.then()` API), which reduces confidence that the full end-to-end PoC as described is reproducible without contradicting its own precondition ("attacker deploys no contract").

### Recommendation
`remove_account` should also enumerate and remove all `TrieKey::PostponedReceipt` entries whose `receiver_id` matches the account being deleted (and similarly for `PromiseYieldReceipt`/`PromiseYieldStatus`/`YieldIdToDataId`/`DataIdToYieldId` and `ReceivedData` entries tied to the account, if not already handled elsewhere), using a prefix iterator analogous to the access-key and contract-data cleanup already present, so that no outstanding receipt can survive account deletion and be replayed against a differently-owned account created later at the same `account_id`.

### Proof of Concept
Not fully reproducible from the codebase evidence gathered: the missing `PostponedReceipt` cleanup in `remove_account` is confirmed by direct code inspection, but constructing the described self-targeted postponed `AddKey` receipt without deploying a contract is not supported by the promise/receipt creation model as understood, and I could not verify (due to tool budget) whether `action_delete_account` blocks deletion while postponed receipts are outstanding. A valid PoC would need a runtime/test-loop integration test that: (1) deploys a contract on `victim.near` that issues a promise batch to itself with `AddKey` gated on an unresolved `input_data_id` (contradicting the "no contract" precondition in the question), (2) deletes `victim.near`, (3) has a third party recreate and fund `victim.near`, (4) delivers the matching `DataReceipt`, and (5) asserts the new account's access-key set contains the attacker's key. Given the unresolved precondition conflict and unverified `DeleteAccount` guard, this cannot be certified as a complete, exploitable chain from the evidence available.

### Citations

**File:** core/store/src/utils/mod.rs (L120-143)
```rust
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
