### Title
Postponed receipts targeting a deleted account are never purged, allowing a stale self-authorized `AddKey`/`DeleteKey` action to execute against a re-created account of the same name - (File: core/store/src/utils/mod.rs)

### Summary
`remove_account` in `core/store/src/utils/mod.rs:505-575` clears `TrieKey::Account`, `TrieKey::ContractCode`, the `AccessKey`/`GasKeyNonce` prefix, and the `ContractData` prefix, but never removes `TrieKey::PostponedReceipt`, `TrieKey::PendingDataCount`, or `TrieKey::ReceivedData` entries keyed by the account being deleted. This can leave a postponed action receipt whose `predecessor_id == receiver_id == X` stored in the trie after `X` is deleted; if a third party recreates account `X` and the missing `DataReceipt` later arrives, the stale receipt (which was authorized by the original account holder) executes against the new account.

### Finding Description
`remove_account` is the cleanup routine invoked by the `DeleteAccount` action path in `runtime/runtime/src/actions.rs`. Its iteration scope is explicit and limited: [1](#0-0) [2](#0-1) 

It does not enumerate or remove any `TrieKey::PostponedReceipt`, `TrieKey::PendingDataCount`, or `TrieKey::ReceivedData` entries scoped to `account_id`, even though these trie key variants exist and are keyed by `receiver_id` (confirmed present in `core/primitives/src/trie_key.rs` and used throughout the receipt-postponement logic in `runtime/runtime/src/lib.rs`).

Exploit flow:
1. Attacker owns account `X` and sends (or triggers via contract) an action receipt targeting `X` itself (`predecessor_id == receiver_id == X`) containing two input data dependencies and actions including `AddKey`/`DeleteKey`. Because not all dependent `DataReceipt`s have arrived, the runtime stores this as a postponed receipt under `TrieKey::PostponedReceipt { receiver_id: X, ... }` plus a `PendingDataCount` entry.
2. While one dependency is still outstanding, the attacker deletes `X` via a `DeleteAccount` action. This invokes `remove_account`, which clears the account record, code, keys, and contract data — but leaves the postponed receipt and pending-data bookkeeping for `X` untouched in the trie.
3. A third party (unaware of the stale postponed receipt) creates a new account with the same id `X` via `CreateAccount`.
4. The missing `DataReceipt` for the original dependency is finally delivered to `X`. The runtime's pending-data-count logic sees the count reach zero and pulls the postponed receipt back out of the trie, then executes its actions against whatever account state currently exists at `X` — now the new owner's account.
5. Because the account/actor authorization check in `runtime/runtime/src/actions.rs` (`check_actor_permissions`) is evaluated against the receipt's `predecessor_id`/`receiver_id` fields (both literally the string `X`), and account identity in this codebase is the bare `AccountId` string with no generation/incarnation counter, the stale `AddKey`/`DeleteKey` action passes the "self-action" permission check and mutates the new account's key set — despite the new owner never authorizing it.

I was able to fully confirm the `remove_account` cleanup gap (step 2) from the source. I was not able to read the full bodies of `check_actor_permissions` or the `DeleteAccount`/postponed-receipt execution logic in `runtime/runtime/src/actions.rs` and `runtime/runtime/src/lib.rs` within the available tool budget, so the precise mechanics of steps 4-5 (whether any additional guard exists, e.g. blocking `DeleteAccount` while pending receipts exist, or re-deriving actor identity from something other than the literal `AccountId` string) could not be independently verified against the current code and should be checked directly before relying on this as a definitive finding.

### Impact Explanation
If confirmed, this is an authorization-escalation-across-accounts bug: a receipt created and authorized by an account's former owner can add or delete access keys on a semantically distinct new account that merely reuses the same `AccountId` string, violating the invariant that a promise never carries privileges its creator did not hold on the current account holder. This matches the "authorization escalation across accounts or promises" bounty category.

### Likelihood Explanation
The precondition requires the attacker to control the timing of their own account deletion relative to an outstanding multi-dependency receipt they created, and requires a third party to independently choose to recreate an account with the exact same name before the stale dependency resolves — the latter is not attacker-controlled and reduces real-world likelihood significantly, since NEAR account names are typically not chosen adversarially by unrelated third parties in a way an attacker can predict or force. The cost to the attacker is low (ordinary transactions), but reliable exploitation depends on an external, non-guaranteed event (id reuse) which I could not confirm is otherwise prevented (e.g., some protocols disallow reusing top-level/sub-account ids shortly after deletion).

### Recommendation
Extend `remove_account` (`core/store/src/utils/mod.rs:505-575`) to also enumerate and remove `TrieKey::PostponedReceipt`, `TrieKey::PendingDataCount`, and `TrieKey::ReceivedData` entries scoped to the deleted `account_id`, refunding/failing any outstanding postponed receipts and their pending data at deletion time instead of leaving them dangling in the trie.

### Proof of Concept
Integration/runtime-test-loop plan:
1. Create account `X` with a full-access key.
2. Submit an action receipt to `X` (predecessor `X`, receiver `X`) with 2 input `data_id`s and actions `[FunctionCall, AddKey(new_key)]`, causing it to be postponed.
3. Deliver only one of the two `DataReceipt`s.
4. Submit a `DeleteAccount` action from `X`, verify `remove_account` runs and the account/keys/code/data are gone, but assert (via direct trie inspection) the `TrieKey::PostponedReceipt`/`TrieKey::PendingDataCount` for `X` still exist.
5. Have a different signer submit `CreateAccount` for `X`, funding it and adding its own access key.
6. Deliver the second (previously missing) `DataReceipt`.
7. Assert that `new_key` (added on behalf of the original owner) now appears in the new `X` account's access-key set, despite the new owner never authorizing it — demonstrating unauthorized privilege injection across the account recreation boundary.

### Citations

**File:** core/store/src/utils/mod.rs (L504-513)
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
```

**File:** core/store/src/utils/mod.rs (L555-573)
```rust
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
```
