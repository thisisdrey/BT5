### Title
Deleting an account with outstanding postponed action receipts orphans `PostponedReceipt`/`PostponedReceiptId`/`PendingDataCount` trie entries, enabling authorization escalation on account re-creation - (File: core/store/src/utils/mod.rs)

### Finding Description
`remove_account` in `core/store/src/utils/mod.rs` is the single place `action_delete_account` (in `runtime/runtime/src/actions.rs`) calls to purge an account's trie state. Inspecting it directly shows it only removes:
- `TrieKey::Account`
- `TrieKey::ContractCode`
- all `TrieKey::AccessKey` / gas-key-nonce entries under the account's access-key prefix
- all `TrieKey::ContractData` entries under the account's data prefix [1](#0-0) 

It never enumerates or removes `TrieKey::PostponedReceipt { receiver_id, .. }`, `TrieKey::PostponedReceiptId { receiver_id, .. }`, or `TrieKey::PendingDataCount { receiver_id, .. }` for the account being deleted. These keys are written earlier by `process_action_receipt` whenever a receipt targeting the account is postponed awaiting input data (the classic case of a `Promise.then()` cross-contract callback). If an account with such postponed state is deleted via `DeleteAccountAction`, the postponed receipt (including its full `Receipt` body — actions, `predecessor_id`, `signer_id`) survives in the trie under a key still indexed by the now-deleted `receiver_id`.

If a new, unrelated party later creates an account with the exact same `AccountId` (permissible any time after deletion since the account no longer exists in state, and for subaccounts of publicly-registrable namespaces this requires no privileged access), the dangling `PostponedReceiptId`/`PendingDataCount` state is silently inherited by the new account. When the originally-awaited `DataReceipt` eventually arrives, `process_receipt`'s `Data` branch in `runtime/runtime/src/lib.rs` looks up `PostponedReceiptId` by `receiver_id`, finds the surviving entry, decrements `PendingDataCount`, and once it hits zero, retrieves and executes the stale `Receipt` via `apply_action_receipt` — with the receipt's original `predecessor_id`/`signer_id` (the old account) still embedded as `actor_id`. Any privileged action in that stale receipt (`AddKeyAction`, `DeleteKeyAction`, `DeployContractAction`, `StakeAction`) then executes against the *new* owner's account, entirely without the new owner ever having signed or consented to it — violating the invariant that a promise never carries privileges beyond what its creator held at execution time.

I was not able to fully inspect the complete body of `action_delete_account` (only its declaration was located due to tool limitations) to confirm there is no separate guard rejecting deletion while postponed/pending receipts exist for the account; based on the confirmed contents of `remove_account` — the actual state-cleanup routine — no such cleanup happens at the storage layer regardless.

### Impact Explanation
This is an authorization-escalation vulnerability across accounts: a resurrected `PostponedReceipt` executes actions attributed to a stale `predecessor_id`/`actor_id` against a newly created, unrelated account, letting privileged actions (key/contract/stake mutations) apply without the new account owner's consent. This falls under the "authorization escalation across accounts or promises" bounty category.

### Likelihood Explanation
Preconditions are attacker-affordable and repeatable: fund an account, deploy a trivial contract that issues two `Promise.then()` cross-contract calls to itself, submit `DeleteAccountAction` before the callback data arrives (staking balance is zero so `DeleteAccountStaking` check passes), then have any third party re-create the identically-named account (feasible for accounts under a publicly registrable namespace, e.g. via NEAR's `near` top-level registrar contract). No validator, node-operator, or leaked-key access is required — only normal RPC transaction submission. The race window (submit delete before the awaited receipt is delivered) is fully attacker-controlled since the attacker chooses when to submit `DeleteAccountAction` relative to their own outstanding promise.

### Recommendation
`remove_account` should, before removing the account, enumerate and delete any `TrieKey::PostponedReceiptId`, `TrieKey::PendingDataCount`, and `TrieKey::PostponedReceipt` entries keyed by the account's `receiver_id` (and drop the corresponding `ReceivedData` entries), or `action_delete_account` should reject `DeleteAccountAction` outright if the account still has outstanding postponed/pending receipts — mirroring the pattern used for `DeleteAccountStaking` checks.

### Proof of Concept
Runtime/apply-path integration test:
1. Deploy a contract on `victim.near` and call a method that issues two `Promise.then()` cross-contract calls to itself, producing a postponed receipt keyed by `TrieKey::PostponedReceipt{receiver_id: "victim.near", ..}` plus `PendingDataCount`/`PostponedReceiptId` entries.
2. Before the callback `DataReceipt`s are delivered, submit a transaction with `DeleteAccountAction{beneficiary_id: "attacker.near"}` on `victim.near` and apply it.
3. Assert via trie iteration that `TrieKey::PostponedReceipt{receiver_id: "victim.near", ..}` is non-empty (demonstrating `remove_account` failed to clean it up).
4. Re-create `victim.near` with a fresh key controlled by a new owner via `CreateAccountAction` (unprivileged registrar path).
5. Deliver the original `DataReceipt` and let `process_receipt`'s `Data` branch run `apply_action_receipt` on the stale receipt.
6. Assert the new owner's access keys / contract / stake state were mutated despite never being signed by the new owner — confirming the escalation.

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
