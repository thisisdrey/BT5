### Title
Stale postponed-receipt / data-dependency state on a deleted account can trigger unauthorized `AddKey` on a same-named recreated account - (`core/store/src/utils/mod.rs`)

### Finding Description
`remove_account` in `core/store/src/utils/mod.rs:505-575` is the routine `action_delete_account` (`runtime/runtime/src/actions.rs:314-390`) calls to purge an account: it removes `TrieKey::Account`, `TrieKey::ContractCode`, all `AccessKey`/gas-key entries, and all `ContractData` entries for `account_id`. [1](#0-0) [2](#0-1) 

It does **not** remove any of the receipt-matching bookkeeping that is also keyed by `account_id`: `TrieKey::PostponedReceipt{receiver_id, receipt_id}`, `TrieKey::PostponedReceiptId{receiver_id, data_id}`, `TrieKey::PendingDataCount{receiver_id, receipt_id}`, or `TrieKey::ReceivedData{receiver_id, data_id}`. These are exactly the structures used by `process_action_receipt`/`process_receipt` to postpone an `ActionReceipt` until its `input_data_ids` arrive, and to resume/execute it once satisfied. [3](#0-2) [4](#0-3) 

Because these keys are namespaced only by `account_id` (not by any per-incarnation generation counter), if an account that has a receipt postponed on it (waiting for a data dependency) is deleted before that dependency resolves, the postponed state survives in the trie. If the same account name is later recreated (e.g. a deleted top-level `*.near` name being re-registered, which the protocol permits since `CreateAccount` only checks that the name is not currently occupied, not whether it was previously used), the still-pending `DataReceipt` for the original attacker-controlled data dependency will eventually arrive, decrement `PendingDataCount` to zero, and cause the runtime to fetch and execute the stale `PostponedReceipt` (`runtime/runtime/src/lib.rs:1431-1455`) against whatever account currently occupies that name — i.e. the victim's freshly created and funded account.

The postponed `ActionReceipt`'s actions (e.g. `AddKeyAction{full_access}`) execute with `actor_id` derived from the receipt's own `predecessor_id`, which for a self-scheduled batch equals the account name itself. Since `predecessor_id == receiver_id` still holds after recreation (same string), the runtime's self-authorization check for adding a full-access key is satisfied even though the account is a completely different entity (different owner, different keys) than when the receipt was originally created. The attacker fully controls both halves of the exploit: the postponed `AddKey` receipt and the timing of the satisfying `DataReceipt` (via a nested cross-contract call chain they construct themselves), giving them latitude to delay resolution until after a targeted account name is recreated.

### Impact Explanation
This is an authorization-exactness violation: an entity with no legitimate relationship to a newly created account can smuggle a full-access key onto it via receipt-matching state left over from that account's *previous* deleted incarnation. Once the attacker holds a full-access key on the victim's account, they can immediately submit a `Transfer` (or drain via `FunctionCall`) to steal the victim's freshly deposited balance — theft of funds via unauthorized full-access key grant, matching the "authorization escalation across accounts / theft of funds" bounty category.

### Likelihood Explanation
This requires the attacker to control (own, or at least control the deletion of) the account name in question before the victim registers it, and to time an in-flight cross-contract call chain so its final `DataReceipt` lands after the victim's `CreateAccount`+deposit. This is realistic for **name-squatting scenarios**: an attacker registers a desirable/short/branded top-level name, plants a self-postponed `AddKey` receipt gated on a data dependency they control, deletes the account, and waits — potentially over an extended period — for a future owner (the "victim") to register that exact name, at which point the attacker completes the pending call chain to trigger the `AddKey`. It does not require any validator, node-operator, or network-layer privilege — only ordinary transaction submission from an unprivileged account, satisfying the threat model. The main constraint is targeting a specific account name and reasonably estimating/controlling when a victim will (re)create it, which is feasible for popular/short names but not universally exploitable against arbitrary users.

### Recommendation
When an account is deleted, purge all receipt-matching state associated with its name, not just `Account`/`ContractCode`/`AccessKey`/`ContractData`: iterate and remove `PostponedReceipt`, `PostponedReceiptId`, `PendingDataCount`, and `ReceivedData` entries for `account_id` inside `remove_account` (or explicitly in `action_delete_account` before returning). Alternatively/additionally, tie postponed-receipt resolution to an account "incarnation" identifier (e.g., include a monotonic per-account creation counter in the postponed-receipt/pending-data keys, bumped on every `CreateAccount`) so that a data receipt satisfying a dependency created under a previous incarnation can never resume execution against a newer one.

### Proof of Concept
Integration test in the `runtime/runtime` apply-path (similar harness to `test_function_call_after_same_chunk_delete_recreate_resolves_fresh_code` in `runtime/runtime/src/tests/apply.rs`):
1. Create account `A` (attacker-controlled).
2. From `A`, submit a receipt that issues a cross-contract call to another attacker-controlled contract `X` and, via `.then()`, schedules a batch on `A` itself containing `AddKeyAction{full_access, attacker_pubkey2}` with `input_data_ids = [data_id_of_X_call]`. Apply this receipt/chunk without ever delivering the `DataReceipt` from `X` — leaving the `AddKey` action receipt postponed in state (assert `PostponedReceipt`/`PendingDataCount` trie entries exist for `A`).
3. Submit and apply a `DeleteAccountAction` receipt for `A` (self-delete, beneficiary attacker). Assert `Account`, `AccessKey` records for `A` are gone, but the postponed-receipt trie keys for `A` remain (demonstrating the gap in `remove_account`).
4. Simulate the victim: submit `CreateAccountAction` + initial deposit `Transfer` for account name `A` from an unrelated victim signer/key. Record `Account.amount` for `A` after this step.
5. Now deliver the pending `DataReceipt` (data_id matching step 2) to account `A`. Assert the postponed `AddKeyAction` executes and `get_access_key(A, attacker_pubkey2)` now returns `Some(FullAccess)`.
6. Sign and submit a `Transfer` from `A` using `attacker_pubkey2` draining the balance to an attacker-controlled account.
7. Assert `Account.amount` for `A` before step 6 vs. after step 6 shows the victim's deposit was stolen, and assert this is possible only because of the leaked key — i.e., fail the test if `get_access_key(A, attacker_pubkey2)` is `None` after step 3 (expected fix behavior: postponed receipt/data-dependency state must not survive account deletion).

### Citations

**File:** core/store/src/utils/mod.rs (L505-513)
```rust
pub fn remove_account(
    state_update: &mut TrieUpdate,
    account_id: &AccountId,
) -> Result<RemoveAccountResult, StorageError> {
    state_update.remove(TrieKey::Account { account_id: account_id.clone() });
    state_update.remove(TrieKey::ContractCode { account_id: account_id.clone() });

    let mut gas_key_nonce_count: usize = 0;
    let mut gas_key_nonce_total_key_bytes: usize = 0;
```

**File:** runtime/runtime/src/actions.rs (L371-389)
```rust
    let remove_result = remove_account(state_update, account_id)?;
    result.tokens_burnt =
        result.tokens_burnt.checked_add(gas_key_balance_to_burn).ok_or_else(|| {
            StorageError::StorageInconsistentState("tokens_burnt overflow".to_string())
        })?;
    if remove_result.gas_key_nonce_count > 0 {
        let compute = storage_removes_compute(
            &config.wasm_config.ext_costs,
            remove_result.gas_key_nonce_count,
            remove_result.gas_key_nonce_total_key_bytes,
            AccessKey::NONCE_VALUE_LEN * remove_result.gas_key_nonce_count,
        );
        result.compute_usage = safe_add_compute(result.compute_usage, compute).map_err(|_| {
            StorageError::StorageInconsistentState("compute_usage overflow".to_string())
        })?;
    }
    *actor_id = receipt.predecessor_id().clone();
    *account = None;
    Ok(())
```

**File:** runtime/runtime/src/lib.rs (L1386-1439)
```rust
            VersionedReceiptEnum::Data(data_receipt) => {
                // Received a new data receipt.
                // Saving the data into the state keyed by the data_id.
                set_received_data(
                    state_update,
                    account_id.clone(),
                    data_receipt.data_id,
                    &ReceivedData { data: data_receipt.data.clone() },
                );
                // Check if there is already a receipt that was postponed and was awaiting for the
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

**File:** runtime/runtime/src/lib.rs (L1609-1655)
```rust
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
