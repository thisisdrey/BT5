### Title
`EntryBytesBudget` reservation is never released when transaction recording fails, permanently shrinking a slot's entry-byte capacity - ([File: runtime/src/bank/entry_bytes_budget.rs])

### Summary
`EntryBytesBudget::reserve()` at [1](#0-0)  permanently adds `bytes` to the `consumed` atomic counter once it succeeds, and there is no corresponding release/unreserve method anywhere in the codebase for this struct. In `Consumer::execute_and_commit_transactions_locked` (`core/src/banking_stage/consumer.rs`), the reservation is taken speculatively for a batch of processed transactions, and if the subsequent PoH `record_transactions()` call fails, the code explicitly rolls back the cost-tracker state via `Self::remove_added_transaction_costs(bank, &transaction_costs)` but never rolls back the entry-bytes reservation that was just taken.

### Finding Description
The relevant sequence is:
1. `bank.entry_bytes_budget().reserve(entry_bytes)` is called and, on success, atomically bumps `consumed` — see [2](#0-1) .
2. `self.transaction_recorder.record_transactions(...)` is then attempted; if PoH recording fails (e.g., `PohRecorderError::MaxHeightReached` due to a race on `record_transactions_summary`, or any other recorder error) the code takes the failure branch:
```rust
if let Err(recorder_err) = recording_result {
    Self::remove_added_transaction_costs(bank, &transaction_costs);
    ...
    return ExecuteAndCommitTransactionsOutput { ... };
}
``` [3](#0-2) 

Notice that `remove_added_transaction_costs` reverses the `CostTracker::try_add()` state (see the well-tested rollback logic in `try_add`/`remove_transaction_cost` at [4](#0-3) ), but nothing analogous reverses the `entry_bytes_budget().reserve(entry_bytes)` call made moments earlier. `EntryBytesBudget` exposes only `reserve()`; there is no `release`/`unreserve`/`give_back` method in its implementation [5](#0-4) , and a repository-wide search found no other place that decrements `consumed`.

This is structurally identical to the ZetaChain bug class described in the external report: a resource is optimistically "minted"/reserved against a hard cap before a downstream operation is known to succeed, and on failure of that downstream operation the reservation is not rolled back, so the consumed amount only ever grows until the cap is hit.

### Impact Explanation
`EntryBytesBudget::slot_limit()` bounds the total serialized entry bytes recordable for a slot (`max_entry_bytes_per_slot`, e.g. 20 MiB in the legacy slot params, see `runtime/src/slot_params.rs`). Every time a batch's `record_transactions` call fails after a successful `reserve()` — for example due to a benign race where the bank is concurrently frozen or the PoH height is reached mid-record — that batch's `entry_bytes` remains permanently counted against the slot's budget even though nothing was recorded into the block. This is not a full duplicate-mint-style economic bug, but it is a real resource-leak DoS surface confined to leader block production: repeated failed reservations reduce the effective entry-byte capacity available to the leader for the remainder of the slot, causing `MaxHeightReached` (`EntryBytesReserveError::ExceedsSlotLimit`) to trigger earlier than it should, throttling/blocking further transaction inclusion for that slot. Because a new `EntryBytesBudget` is created per bank/slot (see `apply_slot_time_runtime_changes`, [6](#0-5) ), the leak resets each slot, bounding worst-case impact to reduced throughput/blocked recording within the current slot rather than a permanent global cap exhaustion — this differs from the ZETA case where the cap was never reset.

### Likelihood Explanation
The failure path is reachable without any special privilege: any transaction batch processed by an unprivileged banking-stage worker can hit `record_transactions` failure after the entry-bytes reservation succeeds, since `bank.freeze_lock()`/height-race checks happen concurrently with recording. The existing test suite (`test_bank_process_and_record_transactions_already_frozen`, `test_process_transactions_returns_unprocessed_txs`) exercises exactly this "PoH recording fails after execution" scenario, and asserts that the cost tracker returns to zero, but no test asserts the entry-bytes budget is restored — supporting that this rollback is indeed missing and untested.

### Recommendation
Add a `release`/`unreserve` method to `EntryBytesBudget` that atomically decrements `consumed` by the previously reserved amount, and call it in the `recording_result` error branch of `execute_and_commit_transactions_locked` alongside `Self::remove_added_transaction_costs`, so a failed `record_transactions` call leaves the entry-bytes budget in the same state as before the reservation, symmetric to how the cost tracker is already rolled back.

### Proof of Concept
Not independently reproduced in this pass (read-only ask-mode investigation); the described PoC would extend the existing `test_process_transactions_returns_unprocessed_txs`/`test_bank_process_and_record_transactions_already_frozen` tests in `core/src/banking_stage/consumer.rs` to additionally assert `bank.entry_bytes_budget()`'s consumed value returns to its pre-batch value after a forced `record_transactions` failure — currently only the cost tracker's return-to-zero is asserted, and no test covers the entry-bytes budget in this failure path.

### Citations

**File:** runtime/src/bank/entry_bytes_budget.rs (L8-42)
```rust
#[derive(Debug)]
pub struct EntryBytesBudget {
    consumed: AtomicU64,
    slot_limit: u64,
}

impl EntryBytesBudget {
    pub const fn new(slot_limit: u64) -> Self {
        Self {
            consumed: AtomicU64::new(0),
            slot_limit,
        }
    }

    pub const fn slot_limit(&self) -> u64 {
        self.slot_limit
    }

    pub fn reserve(&self, bytes: u64) -> std::result::Result<(), EntryBytesReserveError> {
        loop {
            let current = self.consumed.load(Ordering::Acquire);
            let next = current.saturating_add(bytes);
            if next > self.slot_limit {
                return Err(EntryBytesReserveError::ExceedsSlotLimit);
            }

            if self
                .consumed
                .compare_exchange(current, next, Ordering::AcqRel, Ordering::Acquire)
                .is_ok()
            {
                return Ok(());
            }
        }
    }
```

**File:** core/src/banking_stage/consumer.rs (L371-380)
```rust
        let reserved_bytes =
            bank.entry_bytes_budget()
                .reserve(entry_bytes)
                .map_err(|err| match err {
                    EntryBytesReserveError::ExceedsSlotLimit => PohRecorderError::MaxHeightReached,
                });
        let (record_transactions_summary, record_us) = measure_us!(reserved_bytes.map(|_| {
            self.transaction_recorder
                .record_transactions(bank.bank_id(), processed_transactions)
        }));
```

**File:** core/src/banking_stage/consumer.rs (L397-414)
```rust
        if let Err(recorder_err) = recording_result {
            Self::remove_added_transaction_costs(bank, &transaction_costs);

            Self::extend_processed_retryable_transaction_indexes(
                &mut retryable_transaction_indexes,
                &processing_results,
            );

            return ExecuteAndCommitTransactionsOutput {
                cost_model_throttled_transactions_count,
                cost_model_us,
                transaction_counts,
                retryable_transaction_indexes,
                commit_transactions_result: Err(recorder_err),
                execute_and_commit_timings,
                error_counters,
            };
        }
```

**File:** cost-model/src/cost_tracker.rs (L165-239)
```rust
    /// Checks the block and account limits and, if the transaction fits,
    /// adds its cost to the tracker.
    ///
    /// A failed call leaves the tracker equivalent to the pre-call state.
    /// Account costs applied before the failing account are rolled back,
    /// and the block-level state (including the lock free shared `block_cost`)
    /// is only published after every check has passed.
    pub fn try_add(
        &mut self,
        tx_cost: &TransactionCost<impl TransactionWithMeta>,
    ) -> Result<UpdatedCosts, CostTrackerError> {
        let cost = tx_cost.sum();

        if self.block_cost().saturating_add(cost) > self.limits.block_cost {
            // check against the total package cost
            return Err(CostTrackerError::WouldExceedBlockMaxLimit);
        }

        // check if the transaction itself is more costly than the account_cost_limit
        if cost > self.limits.account_cost {
            return Err(CostTrackerError::WouldExceedAccountMaxLimit);
        }

        let allocated_accounts_data_size =
            self.allocated_accounts_data_size + Saturating(tx_cost.allocated_accounts_data_size());

        if allocated_accounts_data_size.0 > self.limits.allocated_data_size {
            return Err(CostTrackerError::WouldExceedAccountDataBlockLimit);
        }

        // Check each account against account_cost_limit and apply the cost in
        // the same lookup. On failure, undo the applied prefix.
        let mut updated_costliest_account_cost = 0;
        for (index, account_key) in tx_cost.writable_accounts().enumerate() {
            let new_account_cost = match self.cost_by_writable_accounts.entry(*account_key) {
                Entry::Occupied(mut entry) => {
                    let new_account_cost = entry.get().saturating_add(cost);
                    if new_account_cost > self.limits.account_cost {
                        None
                    } else {
                        *entry.get_mut() = new_account_cost;
                        Some(new_account_cost)
                    }
                }
                Entry::Vacant(entry) => {
                    // `cost <= limits.account_cost` was checked above, so an
                    // account without chained cost always fits
                    entry.insert(cost);
                    Some(cost)
                }
            };
            let Some(new_account_cost) = new_account_cost else {
                // the first `index` accounts were applied before this failure
                self.roll_back_applied_costs(tx_cost, cost, index);
                return Err(CostTrackerError::WouldExceedAccountMaxLimit);
            };
            updated_costliest_account_cost = updated_costliest_account_cost.max(new_account_cost);
        }

        // every check passed: publish the block-level state
        self.allocated_accounts_data_size = allocated_accounts_data_size;
        self.transaction_count += 1;
        self.transaction_signature_count += tx_cost.num_transaction_signatures();
        self.secp256k1_instruction_signature_count +=
            tx_cost.num_secp256k1_instruction_signatures();
        self.ed25519_instruction_signature_count += tx_cost.num_ed25519_instruction_signatures();
        self.secp256r1_instruction_signature_count +=
            tx_cost.num_secp256r1_instruction_signatures();
        self.block_cost.fetch_add(cost);

        Ok(UpdatedCosts {
            updated_block_cost: self.block_cost(),
            updated_costliest_account_cost,
        })
    }
```

**File:** runtime/src/bank.rs (L4934-4941)
```rust
    /// Applies slot-time changes for runtime-only fields. This function is
    /// expected to be idempotent.
    fn apply_slot_time_runtime_changes(&mut self) {
        self.entry_bytes_consumed =
            EntryBytesBudget::new(self.current_slot_params().max_entry_bytes_per_slot());
        self.apply_cost_tracker_limits_for_active_features();
        self.apply_partitioned_epoch_rewards_config_for_active_features();
    }
```
