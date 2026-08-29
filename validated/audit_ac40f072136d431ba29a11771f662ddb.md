### Title
Postponed-receipt state survives `DeleteAccount` and resurrects against a recreated account via the `Data` receipt branch of `process_receipt` - (`runtime/runtime/src/lib.rs`, `core/store/src/utils/mod.rs`)

### Summary
`action_delete_account` calls `remove_account`, which only removes `TrieKey::Account`, `ContractCode`, access/gas keys, and `ContractData` for the deleted account. It never touches `TrieKey::PostponedReceipt`, `TrieKey::PostponedReceiptId`, `TrieKey::PendingDataCount`, or `TrieKey::ReceivedData`. If a receiver-side action receipt was postponed (waiting on `input_data_ids`) before the account was deleted, all four of these entries survive deletion under the same `receiver_id`. When the account is later recreated under the same `AccountId` and the delayed `Data` receipts finally arrive, `process_receipt`'s `Data` arm blindly re-keys off `receiver_id` alone, finds the surviving `PostponedReceiptId`/`PendingDataCount`, decrements to zero, fetches the surviving `PostponedReceipt`, and executes it via `apply_action_receipt` against the **new** account incarnation.

### Finding Description
`action_delete_account` (`runtime/runtime/src/actions.rs:314-390`) calls `remove_account(state_update, account_id)` (`core/store/src/utils/mod.rs:505-575`). `remove_account` only removes:
- `TrieKey::Account`
- `TrieKey::ContractCode`
- access keys / gas-key nonces (`get_raw_prefix_for_access_keys`)
- `TrieKey::ContractData` [1](#0-0) 

It never iterates or removes `TrieKey::PostponedReceipt`, `TrieKey::PostponedReceiptId`, `TrieKey::PendingDataCount`, or `TrieKey::ReceivedData`, all of which are keyed by `receiver_id` (the `AccountId`), not by any account-incarnation identifier such as a creation nonce.

Prior to deletion, when `A` creates a self-directed action receipt with two unmet `input_data_ids`, `process_action_receipt` writes exactly these surviving entries: `PostponedReceiptId{receiver_id: A, data_id: d1}`, `PostponedReceiptId{receiver_id: A, data_id: d2}`, `PendingDataCount{receiver_id: A, receipt_id}`, and `PostponedReceipt{receiver_id: A, receipt_id}` via `set_postponed_receipt`. [2](#0-1) 

If `DeleteAccount(A)` executes with 0 of 2 dependencies delivered, none of these four rows are cleaned up, since `remove_account` doesn't touch them. `CreateAccount(A)` then recreates a fresh `Account` record, but the surviving rows remain untouched (they are separate trie keys, unaffected by writing a new `Account` value).

When the two delayed `Data` receipts finally arrive (post-recreation), `process_receipt`'s `Data` arm:
1. Writes `ReceivedData{receiver_id: A, data_id}` for the new account.
2. Looks up `TrieKey::PostponedReceiptId{receiver_id: A, data_id}` — finds the surviving link to the old `receipt_id`.
3. Decrements `PendingDataCount`; when it reaches 0, fetches `get_postponed_receipt(state_update, A, receipt_id)` — the surviving old `PostponedReceipt` — and calls `apply_action_receipt` on it. [3](#0-2) 

`apply_action_receipt` executes with `account_id = receipt.receiver_id()` resolved against the **current** (new) account state (`get_account(state_update, account_id)`), and `actor_id = receipt.predecessor_id()` taken verbatim from the resurrected receipt. [4](#0-3) 

Nothing in this path checks that the account that exists now is the same incarnation that existed when the postponed receipt was created — the trie key namespace is purely `AccountId`-scoped, with no incarnation/creation-nonce component, so the check "does this receipt still belong to the account that authored it" simply does not exist.

### Impact Explanation
This is a genuine, code-confirmed "survivor" bug: `remove_account` (`core/store/src/utils/mod.rs:504-575`) omits four state families (`PostponedReceipt`, `PostponedReceiptId`, `PendingDataCount`, `ReceivedData`) from cleanup, and `process_receipt`'s `Data` arm (`runtime/runtime/src/lib.rs:1386-1473`) will happily resurrect and execute an old postponed receipt's actions against whatever account currently occupies that `AccountId`, without any incarnation check. This matches "authorization escalation across accounts or promises": actions authored under one account incarnation execute with the identity/authority context of a later, unrelated incarnation of the same name (e.g., a factory/registrar contract that deterministically recycles subaccount names for successive tenants — a common pattern for escrow/vault subaccounts). A new tenant's account can have arbitrary previously-queued actions (Transfer, FunctionCall, AddKey, DeployContract, DeleteAccount, etc., authored by the *prior* tenant/attacker before their account was deleted) fire against it once the stale dependency chain resolves, without the new incarnation's transaction or consent triggering it.

Note: in the exact self-directed scenario in the question (attacker deletes and recreates their *own* account), the attacker only affects their own account and gains no privilege they didn't already have. The externally-relevant impact requires a scenario where the recycled `AccountId` is later associated with a different party's funds/state (e.g., factory-created subaccounts, deterministic names) before the delayed dependency resolves — the question's preconditions establish the mechanism but not an explicit victim.

### Likelihood Explanation
Preconditions are cheap and fully attacker-controlled: create a 2-dependency self-callback receipt, then `DeleteAccount` before both dependencies deliver (achievable by simply not resolving one dependency promptly, or relying on natural cross-shard/congestion delay for a cross-shard dependency), then `CreateAccount` under the same name. No validator/node access is needed — this is buildable entirely from a wasm contract via `promise_and`/`promise_then`, `promise_batch_action_delete_account`, and `promise_batch_action_create_account`. The mechanism is deterministic and 100% repeatable; only the "cross-tenant victim" framing (needed for concrete bounty-scoped impact) depends on an external contract pattern (subaccount recycling) that is common in factory/registrar designs but not demonstrated as reachable against a specific existing NEAR mainnet contract in this question.

### Recommendation
`remove_account` should also scan and remove `TrieKey::PostponedReceipt`, `TrieKey::PostponedReceiptId`, `TrieKey::PendingDataCount`, and `TrieKey::ReceivedData` prefixed by the account being deleted, mirroring the existing access-key/contract-data cleanup loops. Alternatively/additionally, `process_receipt`'s `Data` arm and `apply_action_receipt` should validate that a resurrected postponed receipt's implicit "account incarnation" (e.g., a monotonically increasing per-account creation counter persisted in the `Account` record) matches the current account before executing it, and discard/refund the postponed receipt as failed if the account was deleted in the interim.

### Proof of Concept
Integration test plan (test-loop or `runtime/runtime/src/tests/apply.rs`-style multi-block `apply` sequence):
1. Deploy a contract to account `A` that, on call, creates an action receipt targeting itself with two unresolved `input_data_ids` (e.g., via `promise_and` composed from two cross-account/self promises never resolved yet), causing `process_action_receipt` to persist `PostponedReceipt`, two `PostponedReceiptId` rows, and `PendingDataCount = 2` for `A`.
2. In the same or a following block, apply a `DeleteAccount(A)` receipt (beneficiary = some other account) with the two dependencies still unresolved; assert via direct trie lookups that `TrieKey::PostponedReceipt{A, receipt_id}`, `TrieKey::PostponedReceiptId{A, d1}`, `TrieKey::PostponedReceiptId{A, d2}`, and `TrieKey::PendingDataCount{A, receipt_id}` are still present after commit, while `TrieKey::Account{A}` is absent.
3. Apply `CreateAccount(A)` (fresh account, no code/keys).
4. Deliver both `Data` receipts (`data_id = d1`, then `d2`) targeting `A`.
5. Assert: (a) the postponed receipt's actions execute (e.g., produce the expected `ExecutionOutcome`/side effects such as a `Transfer` or new receipt) against the account state that exists *after* step 3, (b) `TrieKey::PostponedReceipt`, `PostponedReceiptId`, and `PendingDataCount` are now removed, confirming the resurrection occurred against the recreated account rather than being rejected as stale.

### Citations

**File:** core/store/src/utils/mod.rs (L504-510)
```rust
/// Removes account, code and all access keys and gas keys associated to it.
pub fn remove_account(
    state_update: &mut TrieUpdate,
    account_id: &AccountId,
) -> Result<RemoveAccountResult, StorageError> {
    state_update.remove(TrieKey::Account { account_id: account_id.clone() });
    state_update.remove(TrieKey::ContractCode { account_id: account_id.clone() });
```

**File:** runtime/runtime/src/lib.rs (L791-856)
```rust
        let account_id = receipt.receiver_id();

        let input_size_limit =
            apply_state.config.wasm_config.limit_config.max_receipt_total_input_size;
        let enforce_input_size_limit = ProtocolFeature::ReceiptPromiseInputSizeLimit
            .enabled(apply_state.current_protocol_version);
        let mut total_input_size: u64 = 0;
        if enforce_input_size_limit {
            for data_id in action_receipt.input_data_ids() {
                if let Some(size) = get_received_data_size(state_update, account_id, *data_id)? {
                    total_input_size = total_input_size.saturating_add(u64::from(size));
                }
            }
        }
        let input_size_exceeded = enforce_input_size_limit && total_input_size > input_size_limit;

        // Collecting input data and removing it from the state.
        let promise_results = if input_size_exceeded {
            for data_id in action_receipt.input_data_ids() {
                state_update.remove(TrieKey::ReceivedData {
                    receiver_id: account_id.clone(),
                    data_id: *data_id,
                });
            }
            Arc::from([])
        } else {
            action_receipt
                .input_data_ids()
                .iter()
                .map(|data_id| {
                    let ReceivedData { data } =
                        get_received_data(state_update, account_id, *data_id)?.ok_or_else(
                            || {
                                StorageError::StorageInconsistentState(
                                    "received data should be in the state".to_string(),
                                )
                            },
                        )?;
                    state_update.remove(TrieKey::ReceivedData {
                        receiver_id: account_id.clone(),
                        data_id: *data_id,
                    });
                    match data {
                        // TODO: Going from Vec<u8> to Rc<[u8]> shrinks the
                        // allocated buffer to fit, which may re-allocate if the
                        // capacity > len.
                        // Most likely, capacity == len holds here anyway but it
                        // would be better to use `Rc<u8>` already in `ReceivedData`
                        // and `DataReceipt`.
                        Some(value) => Ok(PromiseResult::Successful(Rc::from(value))),
                        None => Ok(PromiseResult::Failed),
                    }
                })
                .collect::<Result<Arc<[PromiseResult]>, RuntimeError>>()?
        };

        // state_update might already have some updates so we need to make sure we commit it before
        // executing the actual receipt
        state_update.commit(StateChangeCause::ActionReceiptProcessingStarted {
            receipt_hash: receipt.get_hash(),
        });

        let mut account = get_account(state_update, account_id)?;
        let account_did_not_exist = account.is_none();
        let mut actor_id = receipt.predecessor_id().clone();
        let mut result = ActionReceiptResult::new();
```

**File:** runtime/runtime/src/lib.rs (L1386-1455)
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
