## Title
`EntryBytesBudget::reserve` has no release/rollback path, permanently leaking per-slot entry-byte capacity on every failed `record_transactions()` call - (File: `runtime/src/bank/entry_bytes_budget.rs`)

### Summary
`EntryBytesBudget` (a per-slot counter analogous to `idleETH` in the referenced report — a value that tracks "capacity consumed/available" and must be kept in sync with reality) only exposes `reserve()`, which monotonically increases the `consumed` counter via `fetch_add`/CAS. There is no `release`, `unreserve`, or `refund` method anywhere in the type. In `Consumer::execute_and_commit_transactions_locked` (`core/src/banking_stage/consumer.rs`), bytes are reserved from the budget *before* attempting `record_transactions()`, but if recording fails (`PohRecorderError`), only the cost-tracker accounting is rolled back via `Self::remove_added_transaction_costs` — the entry-bytes reservation is never released. [1](#0-0) [2](#0-1) 

### Finding Description
`EntryBytesBudget::reserve` performs an unconditional `consumed.fetch_add`-style CAS increase and returns `Err` only if the *new* total would exceed `slot_limit`; crucially the increment has already only been attempted, but on success it is permanent for the rest of the slot — there is no corresponding decrement operation defined on the struct at all. [3](#0-2) 

In `execute_and_commit_transactions_locked`, the sequence is:
1. Transactions are executed and their costs are added to `cost_tracker` via `try_add_processed_transaction_costs`.
2. `entry_bytes` (the serialized size of all processed transactions) is computed and `bank.entry_bytes_budget().reserve(entry_bytes)` is called to reserve slot-wide entry-byte capacity.
3. `record_transactions` is attempted using that reservation.
4. If `record_transactions` fails (returns `Err(recorder_err)`, e.g. `PohRecorderError::MaxHeightReached` from a race where the bank height advances past `max_tick_height` concurrently with recording), the code explicitly reverts the cost-tracker side effects with `Self::remove_added_transaction_costs(bank, &transaction_costs)`, but the entry-bytes reservation made in step 2 is never released. [2](#0-1) 

This is the same bug pattern as the referenced report: a resource is provisionally taken from a shared, slot-scoped budget (`idleETH` ↔ `entry_bytes_budget().consumed`), the operation that consumed it fails/is undone, and the code correctly reverts *some* of the bookkeeping (cost tracker, like the giant pool's LP token burn) but omits reverting the *other* piece of bookkeeping (entry-bytes reservation, like `idleETH`). The result: the tracked "consumed" total no longer reflects the real state, and capacity is durably lost for the remainder of the slot even though no bytes were actually recorded into any entry.

Unlike `CostTracker`, which exposes and correctly uses a paired `remove()` for every `add`/`try_add` (verified via `add_transaction_execution_cost`/`sub_transaction_execution_cost` and covered by tests such as `test_cost_tracker_remove`/`test_remove_transaction_cost`), `EntryBytesBudget` was implemented with only a one-directional `reserve()` and no rollback API, so callers structurally cannot restore it even if they wanted to. [4](#0-3) 

### Impact Explanation
Every time `record_transactions` fails after a successful `reserve()` — which is a normal, unprivileged-reachable event any time the PoH recorder rejects a batch near the end of a slot (tick height reached, PoH service busy/backpressure, etc.) — the slot's `entry_bytes_budget` permanently loses that many bytes of capacity for the rest of the slot, without any transactions actually being recorded to consume that capacity. Since `EntryBytesBudget` is reconstructed fresh per new bank (`entry_bytes_consumed: EntryBytesBudget::new(...)` in `Bank::new_from_parent`), the leak is bounded to a single slot, but within that slot it can cause banking stage to spuriously reject/retry legitimate transaction batches for the remainder of the slot (`ExceedsSlotLimit` -> `PohRecorderError::MaxHeightReached`) even though the entry byte budget for real recorded data is far from exhausted — reducing the leader's ability to pack transactions into its slot. This is a real, node-local accounting/availability bug (grossly mis-tracked pre-fee-work capacity leading to under-utilized block production), not a consensus-breaking bug, since `EntryBytesBudget` is a local admission-control counter and does not affect the recorded block's validity. [5](#0-4) 

### Likelihood Explanation
Reaching this path requires only ordinary leader operation: `record_transactions` failing after a successful cost-tracker commit is a normal race that the code already anticipates and partially handles (it explicitly calls `remove_added_transaction_costs` for this exact case). Any unprivileged user's transactions being batched near the tail end of a slot (close to `max_tick_height`) can trigger the race repeatedly under load, so the leak is easily and repeatedly triggerable during ordinary block production, without any attacker privilege or special crafted payloads. Severity of each single leak is bounded by the size of the failed batch, but repeated occurrences near slot boundaries can compound within a slot.

### Recommendation
Add a `release`/`unreserve` method to `EntryBytesBudget` that decrements `consumed` (e.g., via `fetch_sub`, saturating at 0), and call it in the error branch of `execute_and_commit_transactions_locked` alongside `Self::remove_added_transaction_costs` whenever `record_transactions` fails after a successful `reserve()`, so the reserved-but-unused bytes are returned to the slot budget just as the cost-tracker entries are.

### Proof of Concept
1. Leader bank nears `max_tick_height`; banking stage begins processing a transaction batch and successfully executes/commits transaction costs to `cost_tracker`.
2. `entry_bytes` for the batch is computed and `bank.entry_bytes_budget().reserve(entry_bytes)` succeeds, incrementing `consumed`.
3. Concurrently, PoH recorder rejects the record call because the bank's tick height has since been reached (`record_transactions` returns `Err(PohRecorderError::MaxHeightReached)` or similar recorder error) — a normal, non-adversarial timing race documented by the existing `bank_already_frozen` handling right above this code path.
4. `Consumer::execute_and_commit_transactions_locked` executes `Self::remove_added_transaction_costs(bank, &transaction_costs)` to revert cost-tracker state, but does not call any equivalent release on `bank.entry_bytes_budget()`.
5. `entry_bytes_budget().consumed` remains permanently inflated by `entry_bytes` for the rest of the slot even though zero bytes were recorded, artificially reducing the slot's remaining `entry_bytes_budget` capacity for all subsequent batches in that slot.

### Citations

**File:** runtime/src/bank/entry_bytes_budget.rs (L1-43)
```rust
use std::sync::atomic::{AtomicU64, Ordering};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EntryBytesReserveError {
    ExceedsSlotLimit,
}

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
}
```

**File:** core/src/banking_stage/consumer.rs (L371-414)
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

**File:** cost-model/src/cost_tracker.rs (L359-373)
```rust
    /// Subtract extra execution units from cost_tracker
    fn sub_transaction_execution_cost(
        &mut self,
        tx_cost: &TransactionCost<impl TransactionWithMeta>,
        adjustment: u64,
    ) {
        for account_key in tx_cost.writable_accounts() {
            let account_cost = self
                .cost_by_writable_accounts
                .entry(*account_key)
                .or_insert(0);
            *account_cost = account_cost.saturating_sub(adjustment);
        }
        self.block_cost.fetch_sub(adjustment);
    }
```
