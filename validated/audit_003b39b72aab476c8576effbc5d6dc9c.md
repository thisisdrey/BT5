### Title
Incomplete `remove_account` cleanup leaves postponed/yielded cross-contract receipt state orphaned, enabling stale receipts to fire against a recreated account - ([File: core/store/src/utils/mod.rs])

### Summary
`remove_account` in `core/store/src/utils/mod.rs` only clears `TrieKey::Account`, `TrieKey::ContractCode`, `TrieKey::AccessKey`/`GasKeyNonce`, and `TrieKey::ContractData` for a deleted account. It never clears `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, or `DataIdToYieldId`, even though `col::COLUMNS_WITH_ACCOUNT_ID_IN_KEY` in `core/primitives/src/trie_key.rs` lists all 12 of these columns as account-scoped. Because these 8 columns key exclusively on `receiver_id`/`account_id` (not on any account "generation" or nonce), an in-flight postponed action receipt or promise-yield receipt addressed to an account survives `DeleteAccount`, and will later be matched/executed against whatever account currently occupies that `account_id`, including a completely different owner/contract created after the deletion.

### Finding Description
`action_delete_account` (`runtime/runtime/src/actions.rs:314-390`) calls `remove_account` (`core/store/src/utils/mod.rs:505-575`) and then sets `*account = None`, treating account deletion as a full reset of that account's state. `remove_account` explicitly removes only: [1](#0-0) 
and access keys/gas keys/contract data via prefix iteration (`core/store/src/utils/mod.rs:516-573`). It does not touch the receipt-matching state written by `process_receipt`/`process_action_receipt`: [2](#0-1) [3](#0-2) 
which stores `PendingDataCount`/`PostponedReceipt` keyed only by `receiver_id`, nor the `PostponedReceiptId`/`ReceivedData` link written on the `Data`-receipt path: [4](#0-3) 
Crucially, when the missing `DataReceipt` later arrives, `process_receipt` looks up `TrieKey::PostponedReceiptId` and `TrieKey::PostponedReceipt` purely by `receiver_id`/`receipt_id`/`data_id` - it never checks that the account still exists or is "the same" account that received the original action receipt: [5](#0-4) 
The same applies to `PromiseYieldReceipt`/`PromiseYieldStatus`/`YieldIdToDataId`/`DataIdToYieldId`, all of which are keyed only by `receiver_id` (`core/primitives/src/trie_key.rs:244-247`, `278-293`) and are likewise skipped by `remove_account`.

Exploit flow: an attacker who fully controls account `X` (any funded account) can (1) receive a legitimate cross-contract call from a third party that becomes postponed on `X` (awaiting a callback `DataReceipt`), (2) delete `X` via `DeleteAccount` (refunding the balance to a beneficiary of the attacker's choosing) while the postponed receipt/pending data-count/data-id links remain in the trie under `X`'s key prefix, (3) immediately recreate `X` via `CreateAccount`/`DeployContract` with attacker-controlled code and keys. When the awaited `DataReceipt` eventually arrives (it is unconditionally matched by `receiver_id`), the now-stale `PostponedReceipt` fires and executes against the *new* incarnation of `X`, not the account that originally received it - despite `TrieKey::Account` having been reset in between. `check_account_existence`/`check_actor_permissions` gate individual actions but do not gate whether the *receiver identity* backing a postponed receipt is still the same logical account; nothing re-validates that invariant at receipt-matching time, and no signature/nonce/access-key check applies to internal receipt delivery.

This is a direct consequence of the writer/cleaner mismatch: if `remove_account` cleaned up all 12 `COLUMNS_WITH_ACCOUNT_ID_IN_KEY` columns (as its doc comment and the column table imply it should), a deleted account's postponed receipts would simply vanish along with the account, and this cross-generation receipt leakage would be impossible.

### Impact Explanation
Concrete scoped impact: authorization escalation across accounts and potential misdirection/theft of value carried by an in-flight cross-contract receipt (deposits/actions attached to the postponed `ActionReceipt`, or the resumed value of a yielded promise) to a party that was never the intended recipient at receipt-creation time. This maps to the "theft of user funds" / "authorization escalation across accounts or promises" NEAR bounty category. It does not directly cause chain-halt or state-root divergence, since both old and new shard replicas apply the same deterministic (buggy) logic.

### Likelihood Explanation
Preconditions: the attacker only needs an ordinary funded, self-owned account and the ability to have a third party (or itself, in a different flow) send it a cross-contract call that becomes postponed (i.e., depends on unarrived input data) - a routine, cheap, and fully attacker-triggerable pattern (e.g., attacker's own contract can initiate the cross-contract call chain that targets its own postponed receipt, or wait for a naturally occurring third-party call). `DeleteAccount` and `CreateAccount` are ordinary unprivileged actions requiring only a full-access key on the account being deleted/created. The race window (delete+recreate before the awaited data/resume arrives) is fully controllable by the attacker since they can also control when they submit the data-producing transaction. This is repeatable at will and costs only standard gas/storage fees.

### Recommendation
Extend `remove_account` in `core/store/src/utils/mod.rs` to also purge, for the given `account_id`/`receiver_id`, all remaining entries in `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, and `DataIdToYieldId` (mirroring the existing prefix-iteration pattern already used for access keys and contract data), so that account deletion is a true clean slate consistent with `col::COLUMNS_WITH_ACCOUNT_ID_IN_KEY`. This requires a protocol-version-gated fix since it changes state layout/behavior at `DeleteAccount` time and must be rolled out with a migration/feature flag like other `actions.rs` fixes (`FixDeleteAccountGlobalContractStorageUsage`).

### Proof of Concept
Integration test plan (Rust, `runtime/runtime` test harness, e.g. `runtime/runtime/src/tests/apply.rs` style):
1. Create account `victim` with a contract that, on `FunctionCall`, issues a promise chain producing a postponed `ActionReceipt` on `victim` awaiting an `input_data_id` (or use `promise_yield_create`).
2. Before the corresponding `DataReceipt`/resume arrives, submit `DeleteAccount{beneficiary}` for `victim`, then `CreateAccount`+`DeployContract` (attacker-controlled code/keys) for the same `account_id`.
3. Assert via `trie.contains_key` for each of the 8 orphaned `TrieKey` variants (`PostponedReceipt`, `PendingDataCount`, `PostponedReceiptId`, `ReceivedData`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, `DataIdToYieldId`) that the entry is still present after the delete+recreate sequence - expected assertion `survivor_count == 0` fails (8 survivors).
4. Deliver the awaited `DataReceipt`/resume and observe that `process_receipt` executes the stale postponed/yield receipt against the newly created `victim` account (verify via execution outcome/receiver contract state that the new contract, not the old, processed the payload), demonstrating the cross-generation execution.

### Citations

**File:** core/store/src/utils/mod.rs (L509-510)
```rust
    state_update.remove(TrieKey::Account { account_id: account_id.clone() });
    state_update.remove(TrieKey::ContractCode { account_id: account_id.clone() });
```

**File:** runtime/runtime/src/lib.rs (L1396-1439)
```rust
                // given data_id.
                // If we don't have a postponed receipt yet, we don't need to do anything for now.
                if let Some(receipt_id) = get(
                    state_update,
                    &TrieKey::PostponedReceiptId {
                        receiver_id: account_id.clone(),
                        data_id: data_receipt.data_id,
                    },
                )? {
                    // There is already a receipt that is awaiting for the just received data.
                    // Removing this pending data_id for the receipt from the state.
                    state_update.remove(TrieKey::PostponedReceiptId {
                        receiver_id: account_id.clone(),
                        data_id: data_receipt.data_id,
                    });
                    // Checking how many input data items is pending for the receipt.
                    let pending_data_count: u32 = get(
                        state_update,
                        &TrieKey::PendingDataCount { receiver_id: account_id.clone(), receipt_id },
                    )?
                    .ok_or_else(|| {
                        StorageError::StorageInconsistentState(
                            "pending data count should be in the state".to_string(),
                        )
                    })?;
                    if pending_data_count == 1 {
                        // It was the last input data pending for this receipt. We'll cleanup
                        // some receipt related fields from the state and execute the receipt.

                        // Removing pending data count from the state.
                        state_update.remove(TrieKey::PendingDataCount {
                            receiver_id: account_id.clone(),
                            receipt_id,
                        });
                        // Fetching the receipt itself.
                        let ready_receipt =
                            get_postponed_receipt(state_update, account_id, receipt_id)?
                                .ok_or_else(|| {
                                    StorageError::StorageInconsistentState(
                                        "pending receipt should be in the state".to_string(),
                                    )
                                })?;
                        // Removing the receipt from the state.
                        remove_postponed_receipt(state_update, account_id, receipt_id);
```

**File:** runtime/runtime/src/lib.rs (L1608-1622)
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
```

**File:** runtime/runtime/src/lib.rs (L1642-1655)
```rust
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
