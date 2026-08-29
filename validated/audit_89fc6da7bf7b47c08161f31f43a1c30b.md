### Title
`remove_account` fails to clear account-scoped `PostponedReceiptId`/`PendingDataCount`/`PostponedReceipt`/`ReceivedData`/`PromiseYield*` trie keys, allowing a re-created account to inherit stale postponed-receipt state - (File: `core/store/src/utils/mod.rs`)

### Summary
`TrieKey::get_account_id` (`core/primitives/src/trie_key.rs:590-619`) classifies `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, and `DataIdToYieldId` as account-scoped keys, but `remove_account` (`core/store/src/utils/mod.rs:505-575`) only removes `Account`, `ContractCode`, `AccessKey`/`GasKeyNonce`, and `ContractData` entries. `process_action_receipt` (`runtime/runtime/src/lib.rs:1593-1658`) writes `PostponedReceiptId` and `PendingDataCount`/`PostponedReceipt` keyed by `receiver_id` whenever a receipt is waiting on input data, so `DeleteAccount` executed while such state is pending leaves it orphaned in the trie under the account-id prefix, ready to be reattached to a later-recreated account of the same name.

### Finding Description
`remove_account` is invoked from `action_delete_account` in `runtime/runtime/src/actions.rs` when an `Account` processes a `DeleteAccount` action. Its implementation only clears four key families: [1](#0-0) [2](#0-1) 

Meanwhile, `process_action_receipt` writes `TrieKey::PostponedReceiptId` and, when data is still missing, `TrieKey::PendingDataCount` plus the full `PostponedReceipt` payload, all keyed on `receiver_id` (the account being torn down): [3](#0-2) 

`TrieKey::get_account_id` confirms these keys (and `ReceivedData`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, `DataIdToYieldId`) are all logically owned by the account: [4](#0-3) 

Because `remove_account` never enumerates or deletes these families, if a `DeleteAccount` action executes while the account still has an outstanding postponed receipt (waiting on cross-contract/cross-shard input data that has not yet arrived), that postponed state survives account deletion. If the same `account_id` is later re-registered via `CreateAccount` (NEAR permits re-using a deleted account name), the stale `PostponedReceiptId`/`PendingDataCount`/`PostponedReceipt` entries remain addressable under that name. When the originally-awaited `ReceivedData` eventually arrives (also keyed by the same `receiver_id`), the runtime's postponed-receipt resolution logic will match it against the leftover `PostponedReceiptId`, decrement the leftover `PendingDataCount`, and eventually execute the leftover `PostponedReceipt` — now against the new account's contract/code/keys — even though neither the new account owner nor the runtime's account-deletion invariants ever intended this receipt to survive teardown.

No existing check in `remove_account` or in `action_delete_account`'s validation path enumerates or blocks deletion based on outstanding postponed-receipt/pending-data-count/promise-yield state for the account, so nothing currently prevents this state leak.

### Impact Explanation
This is an authorization-inheritance issue, not a consensus/state-root divergence: all validators execute the same deterministic logic and reach the same (incorrect) result, so there's no chain split. The impact is that a re-created account can have a previously-postponed receipt "reappear" and execute against it once its dependency resolves, effectively smuggling an action (which could include `Transfer`, `FunctionCall`, or self-call-trusted invocations) into a differently-controlled/differently-coded account without a fresh authorization check tied to the new account's current state. This falls under "authorization escalation across accounts or promises" per the bounty categories, since a stale, previously-issued receipt is inherited by a new account identity.

### Likelihood Explanation
Exploitation requires: (1) an unprivileged attacker controlling account `A` to schedule a cross-contract call from `A` to itself (or to another account they control) whose completion depends on input data that has not yet arrived (e.g., a `.then()` continuation waiting on an in-flight cross-shard/cross-contract callback), (2) issuing `DeleteAccount` for `A` before that dependency resolves, and (3) re-registering `A` via `CreateAccount` before/after the original dependency's data arrives. All three steps are ordinary transactions available to any funded, unprivileged account — no validator, node, or protocol-level access is needed. The timing window (deleting an account mid-flight of an async promise) is attacker-controlled and repeatable, since the attacker chooses both when to schedule the dependent call and when to submit the `DeleteAccount`/`CreateAccount` transactions.

### Recommendation
Extend `remove_account` to enumerate and delete all account-scoped families identified by `TrieKey::get_account_id`, in particular by prefix-iterating and removing `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, and `DataIdToYieldId` entries for the account being deleted, mirroring the existing access-key/contract-data iteration-and-removal pattern already used in the function. Alternatively/additionally, reject `DeleteAccount` while the account has any outstanding postponed receipt or promise-yield state.

### Proof of Concept
1. Unit test in `core/store/src/utils/mod.rs` (or a new test module) enumerating every `TrieKey` variant for which `get_account_id()` returns `Some`, and asserting membership in the set of key families explicitly removed by `remove_account`:
```rust
let written_account_scoped_keys = /* all TrieKey variants where get_account_id() is Some */;
let cleared_account_scoped_keys = /* Account, ContractCode, AccessKey, GasKeyNonce, ContractData */;
assert_eq!(written_account_scoped_keys, cleared_account_scoped_keys);
```
This assertion fails today because `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, `DataIdToYieldId` are account-scoped per `get_account_id` but absent from `remove_account`.
2. Runtime/test-loop integration test: have account `A` send a receipt to itself with an unresolved `input_data_ids` dependency (so `process_action_receipt` writes `PostponedReceiptId`/`PendingDataCount`/`PostponedReceipt` for `A`), submit `DeleteAccount` for `A`, then `CreateAccount` to re-register `A`, then deliver the pending `ReceivedData` for the original `data_id`. Assert that the postponed receipt executes against the newly created account, demonstrating inherited/stale receipt execution.

### Citations

**File:** core/store/src/utils/mod.rs (L509-510)
```rust
    state_update.remove(TrieKey::Account { account_id: account_id.clone() });
    state_update.remove(TrieKey::ContractCode { account_id: account_id.clone() });
```

**File:** core/store/src/utils/mod.rs (L551-573)
```rust
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
```

**File:** runtime/runtime/src/lib.rs (L1608-1655)
```rust
        let mut pending_data_count: u32 = 0;
        for data_id in action_receipt.input_data_ids() {
            if !has_received_data(state_update, account_id, *data_id)? {
                pending_data_count += 1;
                // The data for a given data_id is not available, so we save a link to this
                // receipt_id for the pending data_id into the state.
                set(
                    state_update,
                    TrieKey::PostponedReceiptId {
                        receiver_id: account_id.clone(),
                        data_id: *data_id,
                    },
                    receipt.receipt_id(),
                )
            }
        }

        if pending_data_count == 0 {
            // All input data is available. Executing the receipt. It will cleanup
            // input data from the state.
            return self
                .apply_action_receipt(
                    state_update,
                    apply_state,
                    pipeline_manager,
                    receipt,
                    receipt_sink,
                    instant_receipts,
                    validator_proposals,
                    stats,
                    epoch_info_provider,
                    receipt_to_tx,
                )
                .map(Some);
        } else {
            // Not all input data is available now.
            // Save the counter for the number of pending input data items into the state.
            set(
                state_update,
                TrieKey::PendingDataCount {
                    receiver_id: account_id.clone(),
                    receipt_id: *receipt.receipt_id(),
                },
                &pending_data_count,
            );
            // Save the receipt itself into the state.
            set_postponed_receipt(state_update, receipt);
        }
```

**File:** core/primitives/src/trie_key.rs (L596-617)
```rust
            TrieKey::ReceivedData { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::PostponedReceiptId { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::PendingDataCount { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::PostponedReceipt { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::DelayedReceiptIndices => None,
            TrieKey::DelayedReceipt { .. } => None,
            TrieKey::ContractData { account_id, .. } => Some(account_id.clone()),
            TrieKey::PromiseYieldIndices => None,
            TrieKey::PromiseYieldTimeout { .. } => None,
            TrieKey::PromiseYieldReceipt { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::BufferedReceiptIndices => None,
            TrieKey::BufferedReceipt { .. } => None,
            TrieKey::BandwidthSchedulerState => None,
            TrieKey::BufferedReceiptGroupsQueueData { .. } => None,
            TrieKey::BufferedReceiptGroupsQueueItem { .. } => None,
            // Even though global contract code might be deployed under account id, it doesn't
            // correspond to the data stored for that account id, so always returning None here.
            TrieKey::GlobalContractCode { .. } => None,
            TrieKey::GlobalContractNonce { .. } => None,
            TrieKey::PromiseYieldStatus { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::YieldIdToDataId { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::DataIdToYieldId { receiver_id, .. } => Some(receiver_id.clone()),
```
