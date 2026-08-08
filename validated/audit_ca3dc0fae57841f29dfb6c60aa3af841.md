### Title
Entry-bytes budget reservation is not refunded when `record_transactions` fails, permanently shrinking a leader's remaining slot capacity - (File: `core/src/banking_stage/consumer.rs`)

### Summary
`Consumer::execute_and_commit_transactions_locked` (banking stage) speculatively reserves PoH "entry bytes" capacity for a batch *before* knowing whether the recording will succeed, exactly mirroring the `InitialETHCrowdfund.batchContributeFor` pattern of debiting a shared budget ahead of a result check. When the subsequent record call fails, only the cost-tracker charge is rolled back — the entry-bytes reservation is not released, so a real block-production resource is spent on work that was never recorded.

### Finding Description
In `core/src/banking_stage/consumer.rs`, the batch-commit path computes `entry_bytes` for the transactions about to be recorded and reserves that many bytes from the bank's entry-bytes budget before attempting to record: [1](#0-0) 

```
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

This is structurally identical to the reported Solidity flaw: `ethAvailable -= args.values[i]` is decremented (here, `reserve(entry_bytes)` is charged) *before* the outcome of the dependent operation (`contributeFor` / `record_transactions`) is known.

When recording subsequently fails (`recording_result` is `Err`), the failure path only reverses the cost-tracker accounting via `Self::remove_added_transaction_costs(bank, &transaction_costs)`; there is no corresponding call to release/return the bytes that were reserved from `bank.entry_bytes_budget()`: [2](#0-1) 

```
if let Err(recorder_err) = recording_result {
    Self::remove_added_transaction_costs(bank, &transaction_costs);

    Self::extend_processed_retryable_transaction_indexes(
        &mut retryable_transaction_indexes,
        &processing_results,
    );

    return ExecuteAndCommitTransactionsOutput {
        ...
        commit_transactions_result: Err(recorder_err),
        ...
    };
}
```

Note that `remove_added_transaction_costs` only touches `bank.write_cost_tracker()`: [3](#0-2) 

```
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

No symmetric call exists to give back the `entry_bytes` that were reserved from `bank.entry_bytes_budget()`. This exactly mirrors the vulnerable pattern described in the external report: a shared, decrementing budget is charged optimistically for a sub-operation, but the charge is only reversed on some failure branches (here, ping/handle_repair-style early failures elsewhere are correctly refunded in other subsystems, e.g., `TokenBucket`-based budgets in `serve_repair.rs`), while this particular failure branch — the one that matters most, a hard recording failure — leaves the debit in place permanently for the remainder of the slot.

### Impact Explanation
`entry_bytes_budget` enforces `EntryBytesReserveError::ExceedsSlotLimit`, i.e., it is the mechanism gating how much serialized transaction data may be packed into entries for the current slot. Every reservation that is charged but never consumed (because recording failed and the transactions are pushed back onto the retryable path) permanently reduces the amount of real capacity available to record other transactions later in the same slot. Because `MaxHeightReached` triggers precisely when the budget is tight (near the end of a slot, or when the recorder is unexpectedly busy/erroring), this bug causes the leaker to spuriously reach `MaxHeightReached` sooner and more often than warranted, dropping/deferring transactions into retries and reducing block-space utilization for that slot. Under repeated recording failures (e.g., transient contention or PoH backpressure), the effect compounds within the slot, degrading the leader's own throughput — a resource-underpricing/quality-of-service degradation of the leader's block production, self-inflicted but externally triggerable to the extent that submitting large batches near height limits increases the odds of the reservation being wasted.

### Likelihood Explanation
The reservation happens on the normal batch-commit path for every processed batch, so any batch that hits a legitimate recording failure (most commonly `EntryBytesReserveError::ExceedsSlotLimit` mapping to `PohRecorderError::MaxHeightReached`, or other Poh backpressure/height errors) exercises this bug. This does not require malicious input — it is a plain accounting asymmetry that fires under ordinary operating conditions once the recording call can fail, and the leader has no self-healing path to reclaim the reserved bytes until the bank/slot rotates.

### Recommendation
Track the amount reserved from `bank.entry_bytes_budget()` for the batch and explicitly release/return it in the `Err(recorder_err)` branch of `execute_and_commit_transactions_locked`, symmetric to how `remove_added_transaction_costs` rolls back the cost tracker. Ensure the `EntryBytesBudget` type exposes (or is given) a corresponding "release"/"unreserve" API for this purpose, and add a regression test asserting that a failed `record_transactions` call leaves `entry_bytes_budget()`'s available capacity unchanged, mirroring the existing `test_cost_tracker_try_add_is_atomic`-style rollback tests already present for the cost tracker.

### Proof of Concept
Not independently executable within this analysis (no code execution/build environment available); the flaw is demonstrated statically by the code excerpts above: `reserve(entry_bytes)` is unconditionally charged before `record_transactions` runs, and the `Err(recorder_err)` handling path only calls `remove_added_transaction_costs` (cost tracker rollback) with no corresponding rollback of `bank.entry_bytes_budget()`. A reproducible test would: (1) construct a bank/consumer test harness (as used in `core/src/banking_stage/consumer.rs` tests, e.g. `setup_test_with_lamports`), (2) force `record_transactions` to return an `Err` (e.g., by exhausting the entry-height limit or injecting a recorder error), and (3) assert that `bank.entry_bytes_budget()`'s remaining capacity before and after the failed call differs by the reserved `entry_bytes`, proving the leak. I was unable to locate/inspect `runtime/src/bank/entry_bytes_budget.rs` in this session to confirm whether an implicit per-call release exists elsewhere; this should be verified by a Devin session with full repository access before treating this as a confirmed, unmitigated bug.

### Citations

**File:** core/src/banking_stage/consumer.rs (L371-381)
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
        execute_and_commit_timings.record_us = record_us;
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
