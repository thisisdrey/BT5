### Title
Stale `PostponedReceipt`/`PendingDataCount` entries survive `DeleteAccount` and execute against a re-created account - (File: core/store/src/utils/mod.rs)

### Finding Description
`remove_account` in `core/store/src/utils/mod.rs` is the function invoked when a `DeleteAccount` action is processed. It explicitly removes `TrieKey::Account`, `TrieKey::ContractCode`, all `TrieKey::AccessKey`/gas-key entries, and all `TrieKey::ContractData` entries for the account: [1](#0-0) 

Nowhere in this function (nor is there any other call site cleaning it up during account deletion) is `TrieKey::PostponedReceipt{receiver_id, receipt_id}` or `TrieKey::PendingDataCount{receiver_id, receipt_id}` removed. Those two keyspaces are written by `set_postponed_receipt`/tracked via the pending-data-count mechanism used by the async-call/data-receipt pipeline: [2](#0-1) 

Because `TrieKey::PostponedReceipt`/`PendingDataCount` are keyed only by `(receiver_id, receipt_id)` and `receiver_id` is just the plain `AccountId`, they are indistinguishable from a namespace perspective before and after an account with the same name is deleted and re-created. NEAR account names carry no "generation"/epoch marker in these trie keys, so a receipt postponed for `victim.near` before deletion will still be found and matched against `victim.near` after the account name is recreated (either by the same attacker as a sub-account owner, or — for top-level `.near` names once they become available again — by an unrelated third party who registers the freed name).

Exploit flow:
1. Attacker's contract on `victim.near` issues an outgoing action receipt with 2+ `input_data_ids` (e.g. `promise_and`), causing the receipt to be postponed: a `PostponedReceipt` and `PendingDataCount` entry are written keyed by the receiver (which can be `victim.near` itself for a self-directed continuation containing e.g. an `AddKey` or `Transfer` action).
2. Attacker submits `DeleteAccount(victim.near)` in an already-included receipt/transaction. `remove_account` clears `Account`/`ContractCode`/`AccessKey`/`ContractData` but leaves the `PostponedReceipt`/`PendingDataCount` rows untouched.
3. The account name `victim.near` becomes available again and is later re-created (by the attacker via a controlled parent account, or by an unsuspecting third party through a registrar for freed top-level names) with entirely new keys/state.
4. The attacker (who controls the timing of the outstanding `DataReceipt`s) delivers the missing data receipts. The runtime decrements `PendingDataCount` to zero and dispatches the stale `PostponedReceipt` for execution, now against whatever account currently occupies `victim.near`.
5. The stale receipt's actions (e.g. `AddKey` granting an attacker-held public key full access, or a `FunctionCall`/`Transfer` with attacker-chosen deposit staged before deletion) execute under the new account's identity, without the new owner ever having authorized them.

No existing check in the account-deletion path validates that an account has no outstanding postponed receipts/pending data before it is deleted, and no check at data-receipt-delivery time verifies that the account backing `receiver_id` is the "same" account (by any generation/liveness marker) that originally created the postponed receipt.

### Impact Explanation
This is an authorization-escalation bug: a receipt (which may contain `AddKey`, `Transfer`, or `FunctionCall` actions with attacker-chosen parameters) staged before account deletion executes later against a freshly re-created account under the same `account_id`, without the new account owner's consent. This matches the "authorization escalation across accounts or promises" bounty category, and in the `AddKey` case can lead directly to permanent compromise/theft of the recreated account's funds.

### Likelihood Explanation
The attacker fully controls the preconditions from an unprivileged client role: they own `victim.near`, deploy the contract that creates the multi-dependency postponed receipt, and control the timing of the `DeleteAccount` receipt and of the subsequent `DataReceipt` deliveries (e.g., by controlling when a cross-contract callback fires). The attacker can also control account re-creation directly if `victim.near` is a sub-account under their own root/TLA; broader third-party impact additionally requires that a fresh owner re-register the exact freed account name, which is opportunistic but realistic for short/valuable freed names. The core defect (asymmetric cleanup in `remove_account`) is deterministic and 100% reproducible regardless of who ends up owning the recreated name.

### Recommendation
`remove_account` (core/store/src/utils/mod.rs) should also enumerate and remove any `TrieKey::PostponedReceipt`/`TrieKey::PendingDataCount` entries keyed by the account being deleted (and their associated `ReceivedData` entries) before the account is allowed to be deleted, or `DeleteAccount` action validation should reject deletion while such outstanding entries exist for the account, refunding/failing the postponed receipt instead of letting it survive account deletion.

### Proof of Concept
Runtime-level test in `runtime/runtime/tests/test_async_calls.rs` style:
1. Deploy a contract on `victim.near` that issues an action receipt requiring 2 input data ids (postponed), targeting `victim.near` itself with an `AddKey` action for attacker's public key.
2. Apply a `DeleteAccount(victim.near)` receipt in the same or next chunk before the data receipts arrive; assert `Account`, `ContractCode`, `AccessKey` are gone from state but `get_postponed_receipt` for that `(receiver_id, receipt_id)` still returns `Some`.
3. Apply `CreateAccount(victim.near)` with a brand-new key set.
4. Deliver the two outstanding `DataReceipt`s; assert the runtime processes the stale postponed receipt and applies its `AddKey` action to the newly created account, i.e. `get_access_key(trie, "victim.near", attacker_pubkey)` returns `Some` even though the new account never authorized it.
5. Unit test `remove_account` directly: create `PostponedReceipt`/`PendingDataCount` entries for an account, call `remove_account`, then assert via `get_postponed_receipt`/`get` on `TrieKey::PendingDataCount` that these entries are still present (documenting the survivor set / asymmetric cleanup).

### Citations

**File:** core/store/src/utils/mod.rs (L120-120)
```rust
pub fn set_postponed_receipt(state_update: &mut TrieUpdate, receipt: &Receipt) {
```

**File:** core/store/src/utils/mod.rs (L505-574)
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
```
