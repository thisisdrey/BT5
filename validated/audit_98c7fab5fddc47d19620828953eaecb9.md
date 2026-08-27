### Title
Duplicate `input_data_ids` in an action receipt cause `PendingDataCount` to permanently desync from `PostponedReceiptId`, freezing the receipt (and any attached deposit) forever - (File: `runtime/runtime/src/lib.rs`)

### Summary
This is a structural analog of the yAxis `Manager` bug: two separate mappings that are supposed to stay in lock-step (`PendingDataCount`, a counter, and `PostponedReceiptId`, a set of per-`data_id` links) can be pushed out of sync by an ordinary, unprivileged action. Once desynced, the counter never reaches zero and the postponed receipt — along with any balance/deposit it carries — is permanently stuck in state, exactly like the yAxis token that got "stuck" in `tokens[_vault]` after `vaults[_token]` was silently overwritten.

### Finding Description
When an `ActionReceipt` arrives and some of its `input_data_ids` are not yet satisfied, `process_action_receipt` builds two independent pieces of bookkeeping from the same `input_data_ids` list: [1](#0-0) 

- For every entry in `input_data_ids` that is still missing, it writes a `PostponedReceiptId { receiver_id, data_id }` entry pointing at the receipt.
- It also increments a local `pending_data_count` by one for every missing entry.

If `input_data_ids` contains the **same `data_id` twice** (both still missing at receipt time), the loop:
1. Writes the `PostponedReceiptId` keyed by `(receiver_id, data_id)` **twice** — the second write simply overwrites the first with an identical value, so there is still only **one** trie entry for that `data_id`.
2. Increments `pending_data_count` **twice**, so the count stored is 2 instead of the "real" number of distinct missing dependencies (1). [2](#0-1) 

That inflated count is persisted as `PendingDataCount`, and the receipt itself is stored as a `PostponedReceipt`.

Later, when the single `Data` receipt for that `data_id` finally arrives, `process_receipt` looks up the (single) `PostponedReceiptId` entry, decrements `PendingDataCount` by exactly one, and only executes/removes the postponed receipt when the counter reaches zero: [3](#0-2) 

Because only one `Data` receipt can ever exist for a given `data_id` (nothing else could produce a second `Data` receipt with the same `data_id` to trigger a second decrement), a `pending_data_count` of 2 that should have been 1 can never reach 0. The `PostponedReceipt`, its `PendingDataCount` entry, and the surviving `PostponedReceiptId` entry become permanently orphaned in state — the receipt (and any NEAR balance/gas refund routed through it) is frozen forever.

Root cause is the absence of a de-duplication check on `input_data_ids`. Validation only checks the *count* of dependencies against a limit, never uniqueness: [4](#0-3) 

And `ReceiptManager::create_action_receipt`, which is the host-function-facing constructor for these receipts (used e.g. by `promise_and`/multi-dependency combinators), accepts `input_data_ids` as given without rejecting or collapsing duplicates: [5](#0-4) 

This mirrors the report's root cause precisely: one mapping (`PostponedReceiptId`, keyed by `data_id`) is idempotent/overwritable, while the other (`PendingDataCount`) is a naive counter derived from the same input without checking for the first mapping's collision — the two pieces of state silently diverge, and cleanup logic (built assuming they are consistent) can no longer reconcile them, mirroring `removeToken`'s cleanup that never runs on the stale entry in the reported bug.

### Impact Explanation
A postponed action receipt that never executes is a receipt whose actions (which can include token transfers, refunds, function calls moving NEAR) never run. Its `PostponedReceipt`/`PendingDataCount`/`PostponedReceiptId` entries remain in state permanently (they are never garbage collected by any other path), which is a permanent freezing-of-funds condition for any deposit attached to that action receipt, and a subtle state-consistency bug that violates the invariant documented in `docs/RuntimeSpec/Receipts.md` (pending count must reach exactly zero when all dependencies are received). This is triggerable by any deployed contract, with no special privileges, purely through the promise/receipt-creation API — squarely in the "action execution and refunds" surface reachable from an ordinary client's contract.

### Likelihood Explanation
Reachability requires only that a contract be able to produce an `ActionReceipt` whose `input_data_ids` contains a duplicate `CryptoHash`. `ReceiptManager::create_action_receipt` (used by the promise combinator that joins multiple promise dependencies into one receipt) takes `input_data_ids: Vec<CryptoHash>` directly from caller-supplied receipt indices, and neither `create_action_receipt` nor `validate_action_receipt` reject duplicates — only a maximum count is enforced. I was not able to directly inspect the exact host-function entry point (e.g. `promise_and`) that maps a contract-supplied list of promise indices to `receipt_indices`/`input_data_ids` in this index, so I cannot state with certainty from the code retrieved whether the host function itself blocks passing the same promise index twice; this is the main open uncertainty. If it does not block it (which is the norm for `promise_and`-style combinators taking a raw index list), the path is fully unprivileged and trivially reachable.

### Recommendation
- In `process_action_receipt` (`runtime/runtime/src/lib.rs`), de-duplicate `input_data_ids` before computing `pending_data_count`, so the counter matches the number of *distinct* missing dependencies rather than the raw list length.
- Alternatively/additionally, reject receipts with duplicate `input_data_ids` at validation time in `validate_action_receipt` (`runtime/runtime/src/verifier.rs`), mirroring the mitigation pattern from the report: check for the invariant before allowing the state to be written, rather than trusting that all consumers of `input_data_ids` treat it as a set.
- Add a regression test that submits an `ActionReceipt`/promise combinator with a duplicated data dependency and asserts the receipt executes exactly once when the single corresponding `Data` receipt arrives (instead of remaining permanently postponed).

### Proof of Concept
Conceptual repro (cannot be fully executed without access to the exact host-function surface for constructing multi-dependency receipts, per the caveat above):
1. From a contract, create two sub-promises `A` and `B`.
2. Use the promise-combinator that calls `ReceiptManager::create_action_receipt` with an `input_data_ids` vector referencing the *same* underlying data id twice — e.g. by combining the same promise index twice (`promise_and(A, A)`), so the generated receipt has `input_data_ids = [data_id_A, data_id_A]`.
3. This receipt is processed by `process_action_receipt`; since `data_id_A` is missing, `pending_data_count` becomes `2` and `PostponedReceiptId{receiver, data_id_A}` is stored once.
4. Promise `A` finally resolves and produces its single `Data` receipt with `data_id_A`.
5. `process_receipt` decrements `PendingDataCount` from 2 to 1 and removes the `PostponedReceiptId` link; since the count is not 0, the postponed receipt is never executed.
6. No further `Data` receipt for `data_id_A` can ever be produced, so `PendingDataCount` stays at 1 forever — the postponed receipt (and any attached actions/deposits) is permanently frozen in state. [1](#0-0) [3](#0-2)

### Citations

**File:** runtime/runtime/src/lib.rs (L1398-1472)
```rust
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
                    } else {
                        // There is still some pending data for the receipt, so we update the
                        // pending data count in the state.
                        set(
                            state_update,
                            TrieKey::PendingDataCount {
                                receiver_id: account_id.clone(),
                                receipt_id,
                            },
                            &(pending_data_count.checked_sub(1).ok_or_else(|| {
                                StorageError::StorageInconsistentState(
                                    "pending data count is 0, but there is a new DataReceipt"
                                        .to_string(),
                                )
                            })?),
                        );
                    }
```

**File:** runtime/runtime/src/lib.rs (L1608-1623)
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

**File:** runtime/runtime/src/verifier.rs (L588-600)
```rust
fn validate_action_receipt(
    limit_config: &LimitConfig,
    receipt: VersionedActionReceipt,
    receiver: &AccountId,
    current_protocol_version: ProtocolVersion,
    mode: ValidateReceiptMode,
) -> Result<(), ReceiptValidationError> {
    if receipt.input_data_ids().len() as u64 > limit_config.max_number_input_data_dependencies {
        return Err(ReceiptValidationError::NumberInputDataDependenciesExceeded {
            number_of_input_data_dependencies: receipt.input_data_ids().len() as u64,
            limit: limit_config.max_number_input_data_dependencies,
        });
    }
```

**File:** runtime/runtime/src/receipt_manager.rs (L111-137)
```rust
    pub(super) fn create_action_receipt(
        &mut self,
        input_data_ids: Vec<CryptoHash>,
        receipt_indices: Vec<ReceiptIndex>,
        receiver_id: AccountId,
    ) -> Result<ReceiptIndex, VMLogicError> {
        assert_eq!(input_data_ids.len(), receipt_indices.len());
        for (data_id, receipt_index) in input_data_ids.iter().zip(receipt_indices.into_iter()) {
            self.action_receipts
                .get_mut(receipt_index as usize)
                .ok_or(HostError::InvalidReceiptIndex { receipt_index })?
                .output_data_receivers
                .push(DataReceiver { data_id: *data_id, receiver_id: receiver_id.clone() });
        }

        let new_receipt = ActionReceiptMetadata {
            receiver_id,
            refund_to: None,
            output_data_receivers: vec![],
            input_data_ids,
            actions: vec![],
            is_promise_yield: false,
        };
        let new_receipt_index = self.action_receipts.len() as ReceiptIndex;
        self.action_receipts.push(new_receipt);
        Ok(new_receipt_index)
    }
```
