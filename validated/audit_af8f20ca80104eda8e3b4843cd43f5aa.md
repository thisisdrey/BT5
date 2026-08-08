### Title
Entry-byte budget never released on `RecordSenderError::Full`/`Disconnected`, permanently wasting slot capacity - ([File: runtime/src/bank/entry_bytes_budget.rs])

### Summary
`Consumer::execute_and_commit_transactions_locked` reserves bytes from the per-slot `EntryBytesBudget` before calling `TransactionRecorder::record_transactions`, but if recording fails with `PohRecorderError::ChannelFull`/`ChannelDisconnected`, only the cost-tracker state is rolled back via `remove_added_transaction_costs`, not the entry-byte reservation. `EntryBytesBudget` exposes no release/rollback method, so the reserved bytes are permanently stuck as "consumed" for the rest of the slot.

### Finding Description
In `core/src/banking_stage/consumer.rs`, bytes are reserved unconditionally before the record attempt: [1](#0-0) 
When `record_transactions` fails, the code explicitly only removes transaction *costs* from the cost tracker, leaving the byte reservation untouched: [2](#0-1) [3](#0-2) 

`EntryBytesBudget::reserve` only ever increments `consumed` via `compare_exchange` and provides no corresponding decrement/release API: [4](#0-3) 

`TransactionRecorder::record_transactions` maps a `RecordSenderError::Full` from the bounded record channel into `PohRecorderError::ChannelFull`, and `RecordSenderError::Disconnected` into `PohRecorderError::ChannelDisconnected`, both of which reach the `Err(recorder_err)` branch in `consumer.rs` above: [5](#0-4) 

The channel becomes `Full` when `RecordSender::try_send` finds `allowed_insertions == 0` on the bounded, capacity-limited record channel shared by all banking-stage worker threads and drained by the PoH service background thread: [6](#0-5) [7](#0-6) 

The budget is only reset when a brand-new `Bank`/slot is created (`EntryBytesBudget::new(...)` in `apply_slot_time_runtime_changes` and `_new_from_parent`), i.e., there is no per-record or per-failure release path within a slot: [8](#0-7) [9](#0-8) 

Because worker threads may concurrently attempt to reserve and record entry bytes while the shared, fixed-capacity record channel is temporarily saturated (multiple `solCoWorker` threads all racing to push batches into one bounded channel drained by a single PoH hashing thread), any transient burst of processed-but-unrecorded work permanently "burns" slot-wide entry-byte capacity with zero bytes actually ever recorded into an entry.

### Impact Explanation
This causes leader entry-byte accounting to diverge from ground truth: `entry_bytes_budget().slot_limit()` can be exhausted by batches that never get recorded as entries, causing all subsequent legitimate batches in the same slot to fail `EntryBytesBudget::reserve` with `ExceedsSlotLimit` (mapped to `PohRecorderError::MaxHeightReached`), effectively ending block production for that slot below its true throughput/byte capacity. This matches "QoS evasion / grossly underpriced pre-fee work" degrading leader capacity — pre-fee-paid processing work (sigverify + execution) that is never committed still consumes a scarce, slot-scoped resource with no path to recovery.

### Likelihood Explanation
Reaching `RecordSenderError::Full` requires the shared record channel's fixed capacity (`BankIdAllowedInsertions::MAX_ALLOWED_INSERTIONS`) to be saturated by in-flight batches from multiple concurrent banking-stage worker threads faster than the single PoH-service thread can drain/hash them. This is a legitimate, code-confirmed backpressure mechanism (also exercised by `test_record_channels` in `poh/src/record_channels.rs`), and it is plausible under sustained high valid-transaction volume with many concurrently active worker threads, though it depends on transient PoH-thread lag rather than being a case the attacker can deterministically force on demand. `RecordSenderError::Disconnected` additionally requires the receiver being dropped, which is a more disruptive/rarer condition. I could not fully verify from the indexed code how much sustained load an unstaked attacker's traffic (after QUIC/stake-weighted QoS throttling upstream of banking stage) can realistically generate to reliably saturate this channel; this remains uncertain without further investigation of TPU-level rate limiting for unstaked senders.

### Recommendation
Add a `release`/`rollback` method to `EntryBytesBudget` that decrements `consumed` (saturating at 0), and call it alongside `Self::remove_added_transaction_costs(bank, &transaction_costs)` in the `Err(recorder_err)` branch of `execute_and_commit_transactions_locked`, releasing exactly `entry_bytes` that were reserved for the failed record attempt.

### Proof of Concept
Rust unit test extension to `runtime/src/bank/entry_bytes_budget.rs` plus an integration-style test in `core/src/banking_stage/consumer.rs`:

1. Unit test demonstrating the missing API:
```rust
#[test]
fn test_reserve_has_no_release_and_leaks_slot_budget() {
    let budget = EntryBytesBudget::new(1_000);
    assert!(budget.reserve(900).is_ok());
    // Simulate a failed record: there is no way to give back the 900 bytes.
    // budget.release(900); // <-- method does not exist
    assert_eq!(budget.consumed.load(Ordering::Acquire), 900);
    // Subsequent legitimate reservation for actually-recorded work now fails
    // even though effectively 0 bytes were ever recorded.
    assert_eq!(budget.reserve(200), Err(EntryBytesReserveError::ExceedsSlotLimit));
}
```

2. Integration test plan for `core/src/banking_stage/consumer.rs` (extending `test_bank_record_transactions`-style setup):
   - Construct a `record_channels(false)` pair and fill the `RecordSender` to capacity for the current `bank_id` (as done in `test_record_channels`) so the next `try_send` returns `RecordSenderError::Full`.
   - Call `Consumer::execute_and_commit_transactions_locked` (or the public `process_and_record_transactions`) with a batch of valid, successfully processed transactions.
   - Assert: `commit_transactions_result` is `Err(PohRecorderError::ChannelFull)`.
   - Assert: `bank.entry_bytes_budget()`'s internal consumed value (exposed via a `#[cfg(test)]` accessor) equals the entry bytes computed for this batch (`SERIALIZED_ENTRIES_OVERHEAD + sum(tx.serialized_size())`), even though no record was ever placed on the channel.
   - Assert: a subsequent call to `bank.entry_bytes_budget().reserve(remaining_capacity + 1)` fails with `ExceedsSlotLimit`, and even a small legitimate reservation matching real recordable work can be rejected once the leaked amount plus genuine usage exceeds `slot_limit()`, demonstrating the permanent, unrecoverable consumption for the remainder of the slot.

### Citations

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

**File:** core/src/banking_stage/consumer.rs (L654-662)
```rust
    fn remove_added_transaction_costs<Tx: TransactionWithMeta>(
        bank: &Bank,
        transaction_costs: &[Option<TransactionCost<'_, Tx>>],
    ) {
        let mut cost_tracker = bank.write_cost_tracker().unwrap();
        for transaction_cost in transaction_costs.iter().flatten() {
            cost_tracker.remove(transaction_cost);
        }
    }
```

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

**File:** poh/src/transaction_recorder.rs (L60-93)
```rust
        if !transactions.is_empty() {
            let (hash, hash_us) = measure_us!(hash_transactions(&transactions));
            record_transactions_timings.hash_us = Saturating(hash_us);

            let (res, poh_record_us) = measure_us!(self.record(bank_id, hash, transactions));
            record_transactions_timings.poh_record_us = Saturating(poh_record_us);

            match res {
                Ok(starting_index) => {
                    starting_transaction_index = starting_index;
                }
                Err(RecordSenderError::InactiveBankId | RecordSenderError::Shutdown) => {
                    return RecordTransactionsSummary {
                        record_transactions_timings,
                        result: Err(PohRecorderError::MaxHeightReached),
                        starting_transaction_index: None,
                    };
                }
                Err(RecordSenderError::Full) => {
                    return RecordTransactionsSummary {
                        record_transactions_timings,
                        result: Err(PohRecorderError::ChannelFull),
                        starting_transaction_index: None,
                    };
                }
                Err(RecordSenderError::Disconnected) => {
                    return RecordTransactionsSummary {
                        record_transactions_timings,
                        result: Err(PohRecorderError::ChannelDisconnected),
                        starting_transaction_index: None,
                    };
                }
            }
        }
```

**File:** poh/src/record_channels.rs (L31-33)
```rust
pub fn record_channels(track_transaction_indexes: bool) -> (RecordSender, RecordReceiver) {
    const CAPACITY: usize = BankIdAllowedInsertions::MAX_ALLOWED_INSERTIONS as usize;
    let (sender, receiver) = bounded(CAPACITY);
```

**File:** poh/src/record_channels.rs (L104-123)
```rust
            // Get the current bank_id and allowed insertions.
            // If there are no allowed insertions, the channel is full - just return immediately.
            // If the `record`'s bank_id is different from the current bank_id,
            // return immediately.
            let current_bank_id_allowed_insertions =
                self.bank_id_allowed_insertions.0.load(Ordering::Acquire);
            let (bank_id, allowed_insertions) = (
                BankIdAllowedInsertions::bank_id(current_bank_id_allowed_insertions),
                BankIdAllowedInsertions::allowed_insertions(current_bank_id_allowed_insertions),
            );

            if bank_id == BankIdAllowedInsertions::DISABLED_BANK_ID {
                return Err(RecordSenderError::Shutdown);
            }
            if bank_id != record.bank_id {
                return Err(RecordSenderError::InactiveBankId);
            }
            if allowed_insertions == 0 {
                return Err(RecordSenderError::Full);
            }
```

**File:** runtime/src/bank.rs (L1492-1492)
```rust
            entry_bytes_consumed: EntryBytesBudget::new(parent.entry_bytes_budget().slot_limit()),
```

**File:** runtime/src/bank.rs (L4936-4941)
```rust
    fn apply_slot_time_runtime_changes(&mut self) {
        self.entry_bytes_consumed =
            EntryBytesBudget::new(self.current_slot_params().max_entry_bytes_per_slot());
        self.apply_cost_tracker_limits_for_active_features();
        self.apply_partitioned_epoch_rewards_config_for_active_features();
    }
```
