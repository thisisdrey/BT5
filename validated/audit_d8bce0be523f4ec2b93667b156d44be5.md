Confirmed: `EntryBytesBudget` only exposes a `reserve` method with no corresponding release/refund method, and grepping the codebase for `entry_bytes_budget`/`EntryBytesBudget`/`release`/`unreserve` shows no code path that decrements `consumed` once it has been incremented. This supports the finding below.

### Title
Entry-bytes slot budget is never released after a failed record, permanently wasting per-slot capacity - (File: `core/src/banking_stage/consumer.rs`, `runtime/src/bank/entry_bytes_budget.rs`)

### Summary
`EntryBytesBudget::reserve` only ever increments its internal `consumed` counter and has no matching decrement/release operation. In `Consumer::execute_and_commit_transactions_locked`, the leader reserves entry bytes for a batch before calling `record_transactions`, but if recording fails, only the cost-tracker's tentative cost additions are rolled back via `remove_added_transaction_costs`; the entry-bytes reservation made moments earlier is never released.

### Finding Description
`EntryBytesBudget::reserve` (`runtime/src/bank/entry_bytes_budget.rs`) does a CAS loop that only grows `consumed` and returns `Err(EntryBytesReserveError::ExceedsSlotLimit)` once the slot limit would be exceeded: [1](#0-0) 
There is no `release`/`unreserve` method anywhere in this struct, and a repo-wide search confirms no other call site decrements `consumed`.

In `core/src/banking_stage/consumer.rs`, the leader reserves `entry_bytes` for a processed batch and only then attempts to record it via `self.transaction_recorder.record_transactions(...)`: [2](#0-1) 

If the record attempt fails for a reason other than `EntryBytesReserveError::ExceedsSlotLimit` (i.e., the reservation itself succeeded but `record_transactions` still returned an `Err`, e.g. `PohRecorderError` variants unrelated to the byte budget), the handling code only unwinds the transaction cost-tracker state and marks transactions retryable — it never releases the entry-bytes reservation that was already consumed: [3](#0-2) [4](#0-3) 

Because `consumed` is monotonically increasing for the lifetime of the `EntryBytesBudget` (which is scoped to a slot/bank), every batch that reserves bytes but subsequently fails to record permanently reduces the remaining entry-byte capacity for the rest of that slot, even though nothing was actually recorded into the block. This mirrors the root cause of the reported bug class: a resource/quota accounting calculation that fails to exclude/release an amount that was provisionally taken but never consumed for its intended purpose (analogous to `depositTokenFlashloanFeeAmount` not being excluded from `excess`), leading here to bytes being "spent" from the budget without ever producing a corresponding recorded entry.

### Impact Explanation
This causes a real accounting bug in the leader's own block-production pipeline: retried transactions (which get resubmitted to `Consumer` after a retryable/failed record) consume additional slot byte-budget on each reservation attempt, without the failed reservations ever being refunded. In a slot with several failed-record attempts (e.g., interleaved with retries, forwarded batches, or bank state races), the leader can exhaust `EntryBytesBudget`'s `slot_limit` well before actually recording that many bytes of real entries, causing further `reserve` calls to fail with `ExceedsSlotLimit` and legitimate transactions to be dropped/retried for the remainder of the slot. This is a self-inflicted DoS/underutilization of block capacity rather than fund loss, but it is a genuine, unbounded (within a slot) resource-leak in a core banking-stage/PoH-recording accounting path.

### Likelihood Explanation
Reaching this path does not require any privileged access — it only requires the batch's `record_transactions` call to fail after a successful `reserve`. This can be triggered by ordinary transaction traffic during any batch whose recording races with leader/bank-freeze transitions, since the code explicitly handles `record_transactions_summary` returning `Err(recorder_err)` as a normal (non-panicking) path and still calls `remove_added_transaction_costs` — but never touches `entry_bytes_budget`. The severity is bounded by slot duration (budget resets with the bank/slot), which reduces (but does not eliminate) the practical exploitation window.

### Recommendation
Add a `release`/`unreserve` method to `EntryBytesBudget` that atomically decrements `consumed` by the previously reserved amount, and call it in the error branch of `Consumer::execute_and_commit_transactions_locked` (alongside `Self::remove_added_transaction_costs`) whenever `record_transactions` fails after a successful `reserve`, so the byte budget accurately reflects only bytes that were actually recorded.

### Proof of Concept
1. Leader processes a transaction batch; `bank.entry_bytes_budget().reserve(entry_bytes)` succeeds, incrementing `consumed`.
2. `self.transaction_recorder.record_transactions(...)` subsequently returns `Err(recorder_err)` for a reason unrelated to the byte budget (e.g., a bank/PoH state transition).
3. `Consumer` handles the error by calling `Self::remove_added_transaction_costs(bank, &transaction_costs)` and marking transactions retryable, but never decrements `entry_bytes_budget().consumed`.
4. Repeating steps 1–3 across retried/failed batches within the same slot accumulates permanently "lost" budget in `EntryBytesBudget`, eventually causing `reserve` to return `Err(ExceedsSlotLimit)` for genuinely recordable, smaller batches later in the same slot — even though far fewer bytes were actually written to the ledger than the configured `slot_limit`.

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
