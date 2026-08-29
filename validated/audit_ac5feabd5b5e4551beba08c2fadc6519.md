### Title
DeleteAccount fails to purge `TrieKey::PostponedReceipt` entries, allowing privileged self-receipts to execute against a re-created account - (File: core/store/src/utils/mod.rs)

### Finding Description
`remove_account` in `core/store/src/utils/mod.rs` deletes `TrieKey::Account`, `TrieKey::ContractCode`, all `TrieKey::AccessKey`/gas-key entries, and all `TrieKey::ContractData` entries for the target account: [1](#0-0) 

It never scans for or removes `TrieKey::PostponedReceipt { receiver_id, .. }` entries belonging to the account being deleted. Postponed receipts are written via `set_postponed_receipt`, keyed purely by `receiver_id` + `receipt_id`, with no linkage back to any specific "incarnation" of the account: [2](#0-1) 

Attack flow:
1. Account A's contract issues a `promise_and` that gates a self-targeted `promise_batch_action_add_key_with_full_access` receipt (receiver_id == predecessor_id == A) on an unresolved input data id. Because the dependency is unresolved, the ActionReceipt is stored postponed under `TrieKey::PostponedReceipt{receiver_id: A, receipt_id}`.
2. Before the dependency resolves, A submits `DeleteAccount` as its only remaining action. `remove_account` wipes A's `Account`, `ContractCode`, `AccessKey`, and `ContractData` records but leaves the postponed `PostponedReceipt` entry intact in the trie, since nothing references it by account existence.
3. A third party (or anyone able to satisfy NEAR's normal account-creation rules for A's namespace, e.g., a top-level name reclaimed through the standard registrar/linkdrop flow) issues `CreateAccount` for A, establishing a brand-new, unrelated account under the same `account_id`.
4. The outstanding dependency resolves, `PendingDataCount` for the postponed receipt hits zero, and the runtime dequeues and executes the postponed ActionReceipt from step 1 against the *new* occupant of A. The `AddKey` (full access) action executes because the account named A now exists and the receipt's `receiver_id` string matches — the trie storage layer has no way to distinguish "the A that authorized this promise" from "the A that exists now."

This breaks the authorization-exactness invariant: the privileged action (adding a full-access key) was authorized by the old contract-controller of A, not by the new owner, yet it silently executes against the new owner's account state.

### Impact Explanation
This is an authorization escalation across accounts (NEAR bounty category: authorization escalation / broken account isolation). The new legitimate owner of account A ends up with an attacker-controlled full-access key injected into their account without ever signing for it, giving the attacker complete control (theft of funds, contract state, and further transactions) over an account they do not own.

### Likelihood Explanation
Preconditions are all reachable by an unprivileged actor: deploy a contract capable of a cross-contract call plus a self-directed `promise_batch_action_add_key_with_full_access`, then submit `DeleteAccount` before the dependent promise resolves — all standard, permitted actions requiring only normal account funding and a full-access key over A (which the attacker legitimately controls before deletion). The only external dependency is that account A's identifier becomes re-registrable by an independent third party after deletion, which is true for NEAR account IDs in general (deleted accounts free their name for future registration). The attack is deterministic and repeatable given control of the timing between `DeleteAccount` and the dependency resolution (attacker fully controls both, since they control when the input data promise resolves and when they submit DeleteAccount).

### Recommendation
When executing `DeleteAccount` (in `runtime/runtime/src/actions.rs::action_delete_account`), either:
- Reject `DeleteAccount` while the account has any outstanding postponed receipts (i.e., non-zero `PendingDataCount`/tracked postponed-receipt count for that account), forcing the caller to wait until all postponed receipts targeting itself are resolved or cancelled; or
- Have `remove_account` (or the delete-account action) enumerate and remove/cancel all `TrieKey::PostponedReceipt` entries keyed to the account being deleted (and correspondingly decrement/clear associated `PendingDataCount` bookkeeping) so that no privileged receipt can survive into a re-created account.

### Proof of Concept
Runtime integration test plan:
1. Deploy a contract on account A that, in one function call, issues `promise_batch_action_add_key_with_full_access` targeting itself (receiver_id = A) gated via `promise_and` on a cross-contract call to another account B that never resolves within the test's timeline (or resolves in a later block).
2. Assert via direct trie access that `TrieKey::PostponedReceipt{receiver_id: A, receipt_id}` exists and `PendingDataCount` for that receipt is > 0.
3. Submit a `DeleteAccount` transaction from A as the only action, and confirm `TrieKey::Account{A}`, `AccessKey`, `ContractCode`, `ContractData` are all removed while `TrieKey::PostponedReceipt{receiver_id: A, receipt_id}` is still present.
4. Submit `CreateAccount` for A from an unrelated party (simulating re-registration), and set an access key controlled by the new owner.
5. Resolve the pending dependency (deliver the outstanding data receipt to make `PendingDataCount` hit 0), advance the chain, and assert that the postponed receipt's `AddKey(FullAccess)` action executes and successfully installs the attacker's public key on the *new* A's account — i.e., `get_access_key(new A, attacker_pubkey)` returns `Some`, despite the new owner never authorizing it.

### Citations

**File:** core/store/src/utils/mod.rs (L120-127)
```rust
pub fn set_postponed_receipt(state_update: &mut TrieUpdate, receipt: &Receipt) {
    assert!(matches!(receipt.versioned_receipt(), VersionedReceiptEnum::Action(_)));
    let key = TrieKey::PostponedReceipt {
        receiver_id: receipt.receiver_id().clone(),
        receipt_id: *receipt.receipt_id(),
    };
    set(state_update, key, receipt);
}
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
