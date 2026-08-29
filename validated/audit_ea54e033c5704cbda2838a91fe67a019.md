## Title
Stale postponed receipt bypasses `remove_account` cleanup, allowing an attacker-crafted `DeleteKeyAction` to strip a FullAccess key from a recreated (implicit) account after `DeleteAccount` — ([File: core/store/src/utils/mod.rs, runtime/runtime/src/lib.rs])

## Summary
`remove_account` (used by `action_delete_account`) only removes `TrieKey::Account`, `ContractCode`, `AccessKey`/gas-key nonces, and `ContractData` for a deleted account, but does **not** remove any `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, or `ReceivedData` entries keyed by that account as `receiver_id`. If an attacker previously arranged for a postponed `ActionReceipt` containing a `DeleteKeyAction` to be parked against their own account (awaiting a data dependency they control the timing of), then deletes that account, the postponed receipt survives the deletion in state untouched. If the same account_id is later recreated (e.g. a NEAR-implicit account re-funded via `Transfer`, which deterministically adds a FullAccess key equal to the account-id-derived public key) and the attacker then releases the pending data, `process_receipt`/`process_action_receipt` in `runtime/runtime/src/lib.rs` will pull the postponed receipt back out of state and execute the embedded `DeleteKeyAction` against the new account's key.

## Finding Description
`action_delete_account` (`runtime/runtime/src/actions.rs:314-389`) calls `remove_account` (`core/store/src/utils/mod.rs:505-575`) to purge account-scoped state on deletion: [1](#0-0) [2](#0-1) 

`remove_account` iterates and removes `Account`, `ContractCode`, all `AccessKey`/gas-key-nonce entries, and `ContractData`. It never touches `TrieKey::PostponedReceiptId`, `TrieKey::PendingDataCount`, `TrieKey::PostponedReceipt`, or `TrieKey::ReceivedData` — all of which are keyed independently by `receiver_id` (the account being deleted) as shown in `core/primitives/src/trie_key.rs:203-219`.

Postponed receipts are created in `process_action_receipt` (`runtime/runtime/src/lib.rs:1593-1658`): when an incoming `ActionReceipt` has unmet `input_data_ids`, the runtime writes a `PostponedReceiptId` per missing `data_id`, a `PendingDataCount`, and the full receipt under `PostponedReceipt` — all namespaced by the `receiver_id`, with no relation to whether that account currently exists. [3](#0-2) 

When a matching `DataReceipt` later arrives, `process_receipt` (`runtime/runtime/src/lib.rs:1367-1474`) looks up `PostponedReceiptId` purely by `(receiver_id, data_id)`, decrements `PendingDataCount`, and once it hits zero, fetches and executes the stored `PostponedReceipt` via `apply_action_receipt` — with **no check that the receiver account still exists or is the same account that existed when the receipt was postponed**. [4](#0-3) 

Exploit flow (attacker acts entirely through their own accounts/contracts):
1. Attacker funds/owns an account `victim` (in the simplest reliable case, a NEAR-implicit account whose account_id literally encodes the public key `K` that will always be (re-)added by any future implicit-account-creation transfer to that id).
2. From a contract they control, the attacker creates a cross-contract promise chain using `promise_batch_then`, so that a receipt targeting `victim` is created with `input_data_ids = [d]` (data not yet available) and `actions = [DeleteKey(K)]` attached via `promise_batch_action_delete_key` (`runtime/near-vm-runner/src/wasmtime_runner/logic.rs:3835-3866`, `runtime/runtime/src/receipt_manager.rs:618-627`). This receipt is postponed against `victim` per the code path above.
3. In a subsequent transaction, the attacker sends `DeleteAccount` on `victim` (beneficiary = attacker's other account). `remove_account` clears the account/keys/contract but leaves the postponed receipt, `PendingDataCount`, and `PostponedReceiptId` entries in trie state, still addressed to `victim`.
4. Later, anyone (a "new owner") sends a `Transfer` to the same implicit account_id `victim`, which does not yet exist; `action_transfer_or_implicit_account_creation` recreates the account and automatically adds the FullAccess key `K` (the account-id-derived key), since implicit-account creation semantics always add that specific key — this is unconditional protocol behavior, not something the new owner controls.
5. The attacker (who alone controls the timing of the pending promise chain from step 2, since it is entirely their own contract logic) finally lets the dependency resolve, delivering the awaited `DataReceipt` for `d` to `victim`. `process_receipt` finds the still-present `PostponedReceiptId`/`PendingDataCount`/`PostponedReceipt` entries, and executes the stale `DeleteKeyAction(K)` against the recreated account, deleting the new owner's only FullAccess key.

No existing check stops this: `action_delete_key` (`runtime/runtime/src/access_keys.rs:52-90`) only checks that the key currently exists on the account (it does), and has no notion of "receipt staleness" or account generation/epoch to detect that the account was deleted and recreated between postponement and execution. `DeleteActionMustBeFinal` and other `DeleteAccountAction` validations (`docs/RuntimeSpec/Actions.md:291-318`) do not require or perform any check for outstanding postponed receipts pointing at the account being deleted.

## Impact Explanation
This causes permanent loss of access to the recreated account for its legitimate new owner: their only (or last) FullAccess key is silently deleted by state left over from a completely unrelated, attacker-controlled receipt chain tied to the account's *previous* incarnation. This matches the "permanent freezing of user funds" bounty category — the new owner's balance becomes inaccessible unless they happen to retain another access mechanism. It also violates the stated liveness/consistency invariant that "a legitimate key add must not be silently undone by pre-existing state," since the account is logically a fresh entity after `DeleteAccount`, yet stale receipt-queue state from the old entity is still authoritative over it.

## Likelihood Explanation
- The attacker needs no special privileges: only the ability to fund/control an account, deploy a contract, and send ordinary transactions (`promise_batch_create`/`promise_batch_then`/`promise_batch_action_delete_key`, `DeleteAccount`) — all standard, unprivileged operations.
- The attacker fully controls: (a) which account_id the postponed `DeleteKey` receipt targets, (b) which key it deletes, and (c) — critically — the timing of when the dependency data is delivered, since the whole promise chain is under the attacker's own contract's control. This directly satisfies "attacker-predictable/attacker-controlled release."
- The scenario is most reliable against NEAR-implicit accounts specifically, because implicit-account (re)creation deterministically installs a FullAccess key equal to the account-id-derived public key, removing any need to guess or race a "new owner"'s chosen key — it's a protocol guarantee. This significantly raises exploitability versus the "guess a reused key" framing in the prompt.
- Repeatable: the attacker can perform this against any implicit account_id whose corresponding private key they hold, deposit funds there to have it recreated as a victim address (if they can convince a third party to treat that address as theirs), or more directly exploit their own re-funded implicit account to demonstrate state corruption even without a distinct "victim."
- Cost is minimal: standard gas fees for a few actions/receipts, no validator or node privileges required.

## Recommendation
When an account is deleted (`action_delete_account` / `remove_account`), also purge all `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `ReceivedData`, and (if applicable) `PromiseYield`/`PromiseYieldTimeout`/`DelayedReceipt` entries keyed by that `receiver_id`, refunding/erroring out any postponed receipts as if their dependency had failed, rather than leaving them dangling to be resurrected against a future, unrelated incarnation of the same account_id. Alternatively, tag postponed-receipt state with an account "generation"/creation nonce and refuse to execute a postponed receipt whose generation does not match the current account's, treating a mismatch as a hard failure with appropriate refunds.

## Proof of Concept
Runtime unit/integration test plan (apply-path, `runtime/runtime/src/tests/apply.rs` style, similar to `test_promise_input_size_limit_exceeded_fails_and_cleans_up`):
1. Set up an account `victim` (or, more convincingly, a NEAR-implicit account whose id is `hex(pubkey K)`), with a FullAccess key `K`.
2. Directly inject state via `set` / trie helpers (mirroring `insert_postponed_action_receipt` in `core/store/src/genesis/state_applier.rs:373-412`) to place a postponed `ActionReceipt` addressed to `victim` with `actions = [DeleteKey(K)]` and `input_data_ids = [d]` where `d` is not yet in `ReceivedData`.
3. Apply a `DeleteAccount` receipt for `victim` (beneficiary arbitrary) and commit.
4. Assert: `get_account(victim)` is `None`, but `get(state, &TrieKey::PostponedReceiptId{receiver_id: victim, data_id: d})`, `TrieKey::PendingDataCount{...}`, and `get_postponed_receipt(state, victim, receipt_id)` are still `Some(...)` (proving the leak).
5. Apply a `Transfer`/implicit-account-creation receipt that recreates `victim` and installs FullAccess key `K` again; assert `get_access_key(victim, K)` is `Some(...)`.
6. Apply a `Data` receipt for `data_id = d` targeting `victim`.
7. Assert the previously postponed `DeleteKeyAction(K)` executes and `get_access_key(victim, K)` becomes `None` — i.e., the new owner's freshly-created key is deleted by state belonging to the deleted, prior account, with no error or rejection produced by the runtime.

### Citations

**File:** core/store/src/utils/mod.rs (L504-509)
```rust
/// Removes account, code and all access keys and gas keys associated to it.
pub fn remove_account(
    state_update: &mut TrieUpdate,
    account_id: &AccountId,
) -> Result<RemoveAccountResult, StorageError> {
    state_update.remove(TrieKey::Account { account_id: account_id.clone() });
```

**File:** core/store/src/utils/mod.rs (L551-574)
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
    Ok(RemoveAccountResult { gas_key_nonce_count, gas_key_nonce_total_key_bytes })
```

**File:** runtime/runtime/src/lib.rs (L1396-1455)
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
                        // Executing the receipt. It will read all the input data and clean it up
                        // from the state.
                        return self
                            .apply_action_receipt(
                                state_update,
                                apply_state,
                                pipeline_manager,
                                &ready_receipt,
                                receipt_sink,
                                instant_receipts,
                                validator_proposals,
                                stats,
                                epoch_info_provider,
                                receipt_to_tx,
                            )
                            .map(Some);
```

**File:** runtime/runtime/src/lib.rs (L1607-1655)
```rust
    ) -> Result<Option<ExecutionOutcomeWithId>, RuntimeError> {
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
