### Title
`EntryBytesBudget` reservation is never released when `record_transactions` fails, permanently wasting per-slot entry-byte budget - (File: `runtime/src/bank/entry_bytes_budget.rs`)

### Summary
`EntryBytesBudget::reserve()` only ever adds to its internal `consumed` counter via `compare_exchange`; there is no corresponding "release"/"unreserve" method. In `Consumer::execute_and_commit_transactions_locked` (`core/src/banking_stage/consumer.rs`), the budget is reserved for the serialized size of the transactions about to be recorded, and then `transaction_recorder.record_transactions(...)` is invoked. If recording fails (e.g. `PohRecorderError`), the error path (`if let Err(recorder_err) = recording_result { ... }`) rolls back the just-added cost-tracker entries via `Self::remove_added_transaction_costs(bank, &transaction_costs)`, but it never rolls back the `entry_bytes_budget` reservation that was just consumed a few lines earlier.

### Finding Description
`EntryBytesBudget::reserve` is a monotonically increasing counter for the current slot: [1](#0-0) 

In `execute_and_commit_transactions_locked`, the reservation is taken before recording, and the resulting `record_transactions_summary` outcome is matched: [2](#0-1) 

When recording fails, only the cost-tracker state is unwound; the just-consumed `entry_bytes_budget` allotment is not: [3](#0-2) 

This mirrors the reported bug class exactly: a resource (in the C4 report, GovNFT funds transferred into `Lock`; here, per-slot "entry bytes" budget) is consumed based on the assumption that a downstream operation (`BondNFT.distribute` / `transaction_recorder.record_transactions`) will succeed and use it, but when that downstream operation silently fails or returns an error, the already-consumed resource is not returned and cannot be recovered — there is no "unreserve" API at all in `EntryBytesBudget`.

`PohRecorderError` (mapped from `EntryBytesReserveError::ExceedsSlotLimit` or returned directly by `record_transactions`) is a normal, frequently occurring condition on a leader — for instance `MaxHeightReached` occurs whenever the bank is already frozen or the PoH tick height is reached while a worker thread is mid-processing a batch (see the test at lines 981-1037 of `consumer.rs`, which demonstrates the already-frozen/`MaxHeightReached` path being hit for ordinary batches, not just malicious ones): [4](#0-3) 

Each time this happens for a batch whose transactions were nevertheless "processed" (thus contributing to `entry_bytes`), the corresponding bytes are permanently subtracted from the slot's entry-byte budget without ever being recorded into any entry. Because `EntryBytesBudget` has no decrement operation, this loss accumulates for the remainder of the slot.

### Impact Explanation
This is reachable purely through normal leader-block-production races (multiple banking-stage worker threads racing against the bank freeze / PoH tick), not through any privileged action — it is a structural gap in accounting, not attacker-controlled input, but it does cause a real, unrecoverable-until-next-slot resource loss: the effective entry-bytes-per-slot capacity for a leader's slot can be silently eaten away by batches that ultimately never get recorded, reducing the amount of transaction data the leader can pack into remaining entries in that slot. This is a real (if narrow) case of grossly-mismatched resource accounting for pre-consensus work, analogous to the underlying C4 finding's "assets get stuck and cannot be recovered."

### Likelihood Explanation
This condition triggers whenever `record_transactions` returns an `Err` (e.g., `MaxHeightReached`) for a batch that had already-processed transactions and thus a nonzero `entry_bytes` reservation. This is a common occurrence near the end of every leader slot as multiple banking-stage worker threads finish processing concurrently with the PoH tick/bank freeze, so the leak is likely to occur on essentially every leader slot's tail end, though the amount leaked per occurrence is bounded by a single batch's `entry_bytes`.

### Recommendation
Add a `release`/`unreserve` method to `EntryBytesBudget` that decrements `consumed` by a given amount (e.g., via `fetch_sub` with saturation), and call it in the `recording_result` error branch of `execute_and_commit_transactions_locked` with the same `entry_bytes` value that was reserved, mirroring the existing `Self::remove_added_transaction_costs` rollback for the cost tracker.

### Proof of Concept
1. On a leader, have banking-stage worker threads process a batch of transactions such that `processed_counts.processed_transactions_count > 0` (transactions pass `load_and_execute_transactions`).
2. Concurrently, let the bank freeze (PoH reaches the last tick) or otherwise cause `transaction_recorder.record_transactions` to return `Err(PohRecorderError::MaxHeightReached)` — this is exactly the scenario already exercised by the existing unit test `test_bank_process_and_record_transactions_already_frozen`, which shows `record_receiver.try_recv().is_err()` (nothing was recorded) while `transaction_counts.processed_count == 1` (the transactions were processed) [5](#0-4) .
3. Before returning the error, `bank.entry_bytes_budget().reserve(entry_bytes)` at line 371-376 of `consumer.rs` has already been called and succeeded, incrementing `consumed`.
4. Because `EntryBytesBudget` exposes no way to decrement `consumed`, and the error-handling block at lines 397-414 only calls `remove_added_transaction_costs` (cost tracker) and not any entry-bytes rollback, the `entry_bytes` reserved for this never-recorded batch remains permanently counted against the slot's `slot_limit`, reducing capacity for subsequent batches in the same slot.

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

**File:** core/src/banking_stage/consumer.rs (L371-395)
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

**File:** core/src/banking_stage/consumer.rs (L981-1037)
```rust
    #[test]
    fn test_bank_process_and_record_transactions_already_frozen() {
        let TestFrame {
            mint_keypair,
            bank,
            bank_forks: _bank_forks,
            record_receiver,
            consumer,
        } = setup_test(None);

        let pubkey = solana_pubkey::new_rand();
        let transactions = sanitize_transactions(vec![system_transaction::transfer(
            &mint_keypair,
            &pubkey,
            1,
            bank.confirmed_last_blockhash(),
        )]);

        bank.freeze();
        assert_ne!(bank.hash(), Hash::default());

        let process_transactions_batch_output =
            consumer.process_and_record_transactions(&bank, &transactions);

        let ExecuteAndCommitTransactionsOutput {
            transaction_counts,
            retryable_transaction_indexes,
            commit_transactions_result,
            ..
        } = process_transactions_batch_output.execute_and_commit_transactions_output;
        assert_eq!(
            transaction_counts,
            LeaderProcessedTransactionCounts {
                attempted_processing_count: 1,
                processed_count: 1,
                processed_with_successful_result_count: 1,
            }
        );
        assert_eq!(
            retryable_transaction_indexes,
            vec![RetryableIndex {
                index: 0,
                immediately_retryable: true,
            }]
        );
        assert_matches!(
            commit_transactions_result,
            Err(PohRecorderError::MaxHeightReached)
        );

        let cost_tracker = bank.read_cost_tracker().unwrap();
        assert_eq!(cost_tracker.transaction_count(), 0);
        assert_eq!(cost_tracker.block_cost(), 0);
        drop(cost_tracker);
        assert!(record_receiver.try_recv().is_err());
        assert_eq!(bank.get_balance(&pubkey), 0);
    }
```
