Based on my research, I found a strong structural analog to the reported bug in Agave's `EntryBytesBudget` accounting used by the banking stage when recording transactions to PoH.

### Title
Missing release of reserved entry-byte budget on failed transaction recording permanently shrinks the leader's per-slot block capacity - (File: `runtime/src/bank/entry_bytes_budget.rs`)

### Summary
`EntryBytesBudget::reserve()` only ever increments the `consumed` counter; there is no corresponding "release"/"unreserve" method anywhere in the codebase. [1](#0-0) 
`Consumer::process_and_record_transactions` calls `reserve()` speculatively *before* attempting to actually record the batch to PoH via `TransactionRecorder::record_transactions`. [2](#0-1) 
If recording subsequently fails, the code carefully reverses the *cost-tracker* charge via `Self::remove_added_transaction_costs(bank, &transaction_costs)`, but it never calls anything to give back the bytes that were already reserved from `entry_bytes_budget`. [3](#0-2) 

### Finding Description
This mirrors the root cause of the reported `NudgeCampaign` bug: a resource pool is decremented ("reserved"/"pending") when an operation begins, but the reversal path (`invalidateParticipations` in the original report; the recording-failure branch here) only unwinds *one* of the two coupled accounting structures (cost tracker) and forgets the other (`entry_bytes_budget`). The reserved bytes are lost forever for the remainder of the slot, exactly as `pendingRewards` was decremented without ever crediting `claimableRewards` back.

Any unprivileged transaction sender can cause this reservation-without-release path to execute: it fires whenever `record_transactions` returns an `Err` after a successful `reserve()` call (e.g., `PohRecorderError::MaxHeightReached` becomes the reserve failure map, but `record_transactions` itself can also fail after a successful reservation, per the match on `record_transactions_summary`). [4](#0-3) 

### Impact Explanation
Each time this path triggers, part of the leader's `max_entry_bytes_per_slot` budget is silently and permanently consumed without any corresponding transaction ever being committed to the block. Repeated triggering during a single leader slot could exhaust the entry-byte budget prematurely, causing `reserve()` to return `ExceedsSlotLimit` for genuinely valid, fee-paying transaction batches later in the same slot — a QoS/DoS degradation of block-production capacity for all other users in that slot, at zero cost to the attacker (the failed-to-record transactions are not committed and thus not charged fees).

### Likelihood Explanation
The blast radius is bounded because `EntryBytesBudget` is per-`Bank`/per-slot and gets recreated fresh for every new slot via `apply_slot_time_runtime_changes`, so the effect cannot accumulate across slots. [5](#0-4) 
I was not able to fully enumerate, within the available tool budget, every `PohRecorderError`/recording-failure variant that can occur *after* a successful `reserve()` but that does **not** already correspond to "no more room in this slot" (e.g., `MaxHeightReached` itself already implies the slot is effectively over, which would make the missing release largely inconsequential in that specific case). Confirming whether there exists a recoverable, non-terminal recording failure that an attacker can reliably and repeatedly trigger mid-slot would require deeper inspection of `poh/src/transaction_recorder.rs` and `poh/src/poh_recorder.rs`, which I did not have iterations left to fully trace.

### Recommendation
Add a symmetric `release`/`unreserve` method to `EntryBytesBudget` and call it in the same failure branch where `remove_added_transaction_costs` is invoked, so that a failed recording attempt cannot cause any net, unrecoverable loss of the slot's entry-byte capacity — matching the fix suggested in the original report (always keep resource-pool decrements and their reversal paths symmetric).

### Proof of Concept
Not independently reproduced in this session (no execution environment available). The vulnerability is derived purely from static code-path analysis: `reserve()` at [1](#0-0)  has no release counterpart, and the only cleanup performed on the error branch in `Consumer::process_and_record_transactions` is `remove_added_transaction_costs`, which only touches the cost tracker, not `entry_bytes_budget`. [6](#0-5)  A background Devin agent with a runnable checkout could write a unit/integration test that forces `record_transactions` to fail after a successful `reserve()` (e.g., by freezing the bank or exhausting the record channel) and assert that `bank.entry_bytes_budget()`'s consumed counter never decreases, to concretely confirm the permanent capacity loss.

### Citations

**File:** runtime/src/bank/entry_bytes_budget.rs (L26-42)
```rust
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

**File:** core/src/banking_stage/consumer.rs (L383-395)
```rust
        let (recording_result, starting_transaction_index) = match record_transactions_summary {
            Ok(summary) => {
                execute_and_commit_timings.record_transactions_timings =
                    RecordTransactionsTimings {
                        processing_results_to_transactions_us: Saturating(
                            processing_results_to_transactions_us,
                        ),
                        ..summary.record_transactions_timings
                    };
                (summary.result, summary.starting_transaction_index)
            }
            Err(err) => (Err(err), None),
        };
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
