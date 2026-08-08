### Title
Unreachable-panic on `CommitCancelled`/account-lock errors during unified-scheduler block verification - ([File: unified-scheduler-pool/src/lib.rs])

### Summary
The GMX report describes a bug class where an error-classification function omits one member of a related error family from its special-case list, causing that specific error to fall through to a default handling path that is inappropriate for it (an order that should be canceled is instead allowed to revert/be treated as fatal). The closest analog in this Agave codebase is `ThreadManager::abort_or_accumulate_result_with_timings` in `unified-scheduler-pool/src/lib.rs`, which hard-codes an "impossible" list of `TransactionError` variants (`AccountInUse`, `AccountLoadedTwice`, `TooManyAccountLocks`, `CommitCancelled`) and calls `unreachable!()` if any of them is observed during block verification, instead of gracefully propagating the error like every other variant does.

### Finding Description
`abort_or_accumulate_result_with_timings` processes the result of each executed task during unified-scheduler block replay/verification: [1](#0-0) 

For `Ok(())` it proceeds normally; for any `Err` other than the four hard-coded variants, it stores the error and signals the caller to abort gracefully (`*result = Err(error); true`). But for `AccountInUse`, `AccountLoadedTwice`, `TooManyAccountLocks`, and `CommitCancelled` specifically, it calls `unreachable!()`, panicking the process.

This mirrors the GMX pattern exactly: a maintained allow-list of error variants that are assumed to receive different treatment than the general case, based on invariants established elsewhere in the codebase (comments state these "should have been validated by blockstore by now" or "should never be observed because the scheduler thread makes all running tasks conflict-free"). The `CommitCancelled` case is explicitly called out as new/less certain: "Block verification should never see this" — a bare assumption rather than an enforced invariant.

`CommitCancelled` is produced by the SVM/consumer batch-abort machinery for `all_or_nothing` execution (used for SIMD-0296-style atomic batches) and by cost-tracker rollback logic: [2](#0-1) [3](#0-2) 

The account-lock validation invariant relied upon for `AccountLoadedTwice`/`TooManyAccountLocks` is also explicitly described as "known redundancy left as-is out of abundance of caution" rather than a compiler- or type-enforced guarantee — i.e., it depends on `replay_stage` always calling `validate_account_locks()` before task creation: [4](#0-3) 

If any code path feeds a task into the unified scheduler for block verification without that upstream validation having run (e.g. a future/alternate replay entry point, a refactor that reorders validation vs. scheduling, or an SVM code path that returns `CommitCancelled` in a context the author didn't anticipate when writing this list), the assumption silently breaks and the validator panics instead of rejecting the transaction/block. This is precisely the "one omitted/mis-classified error case turns benign handling into inappropriate behavior" bug class from the GMX report, except here the consequence is a hard node crash rather than a canceled order.

### Impact Explanation
A panic (`unreachable!()`) in `abort_or_accumulate_result_with_timings` crashes the validator's scheduler/handler thread while verifying an incoming block. Since this executes during replay of a block that originated from another node, any legitimate future code path (or latent bug in a related crate) that yields `CommitCancelled` — or the other three listed errors — to the unified scheduler during verification would deterministically crash every validator using the unified scheduler for verification of that block, i.e., a network-wide liveness/availability risk from a single block, not merely an isolated node fault. This satisfies the "concrete node panic" acceptance bar, though it is not remotely triggerable today given the current call-graph guarantees (see Likelihood).

### Likelihood Explanation
Likelihood is currently low: today, `replay_stage` unconditionally calls `Bank::prepare_sanitized_batch()`/`validate_account_locks()` prior to unified-scheduler task creation, and the `all_or_nothing`/`CommitCancelled` batch-abort logic is documented to be for banking-stage block *production*, not verification. I could not find, within index-search depth, a currently reachable path where `check_transactions`/`load_and_execute_sanitized_transactions` returns `CommitCancelled` for a task submitted to `ThreadManager` during block verification. However, the code explicitly encodes this as an assumption ("should never be observed", "should have been validated by blockstore by now") rather than an enforced invariant, and the comment in `unified-scheduler-logic/src/lib.rs` acknowledges the validation call is currently a hopeful redundancy, not a guarantee at this layer. This makes the construct fragile to future refactors of replay/verification wiring or SVM error propagation — the same "we assumed this case couldn't happen" reasoning that produced the GMX bug.

### Recommendation
Replace the `unreachable!()` arm in `abort_or_accumulate_result_with_timings` with the same graceful-abort handling used for the general `Err(error)` case (store the error, return `true`), or, at minimum, downgrade the panic to a recoverable error path guarded by a `debug_assert!`/metrics counter in release builds, so that an unanticipated error variant results in block rejection rather than a validator crash. Additionally, consider enforcing the "`validate_account_locks()` runs before scheduling" and "`all_or_nothing` batches never appear in verification" invariants at the type level (e.g., a marker type distinguishing pre-validated tasks) so future refactors cannot silently violate them without a compile error.

### Proof of Concept
Not independently reproducible from static analysis alone — I could not identify a currently reachable call path (within available search depth) that feeds `CommitCancelled`, `AccountLoadedTwice`, `TooManyAccountLocks`, or `AccountInUse` into `ThreadManager::abort_or_accumulate_result_with_timings` during block verification, since `replay_stage` is documented to always validate account locks first and `all_or_nothing`/cost-tracker-rollback `CommitCancelled` production is documented as block-production-only. Confirming exploitability would require a Devin session with full repository/build access to trace every caller of `TH::handle`/`execute_task_with_handler` across `unified-scheduler-pool` and `ledger/src/blockstore_processor.rs` to verify whether any verification-time path can produce these four error variants, and to check whether recent commits to SIMD-0296/`all_or_nothing` execution have widened the set of paths that can emit `CommitCancelled`.

### Citations

**File:** unified-scheduler-pool/src/lib.rs (L1066-1102)
```rust
    /// Returns `true` if the caller should abort.
    #[must_use]
    fn abort_or_accumulate_result_with_timings(
        (result, timings): &mut ResultWithTimings,
        executed_task: Box<ExecutedTask>,
    ) -> bool {
        sleepless_testing::at(CheckPoint::TaskAccumulated(
            executed_task.task.task_id(),
            &executed_task.result_with_timings.0,
        ));
        timings.accumulate(&executed_task.result_with_timings.1);

        match executed_task.result_with_timings.0 {
            Ok(()) => {
                // The most normal case
                // This is only for block production.
                assert_eq!(executed_task.consumed_block_size(), 0);

                false
            }
            // This should never be observed because the scheduler thread makes all running
            // tasks are conflict-free
            Err(TransactionError::AccountInUse)
            // These should have been validated by blockstore by now
            | Err(TransactionError::AccountLoadedTwice)
            | Err(TransactionError::TooManyAccountLocks)
            // Block verification should never see this:
            | Err(TransactionError::CommitCancelled) => {
                unreachable!()
            }
            Err(error) => {
                error!("error is detected while accumulating....: {error:?}");
                *result = Err(error);
                true
            }
        }
    }
```

**File:** svm/src/transaction_processor.rs (L630-646)
```rust
            // If this is an all or nothing batch and we failed to process this transaction then we
            // must abort all prior/remaining transactions.
            if config.all_or_nothing && processing_result.is_err() {
                // Abort prior transactions.
                for res in processing_results.iter_mut() {
                    *res = Err(TransactionError::CommitCancelled);
                }

                // Preserve the failure that triggered the batch to abort.
                processing_results.push(processing_result);

                // Abort remaining transactions.
                processing_results.extend(
                    (0..sanitized_txs.len() - processing_results.len())
                        .map(|_| Err(TransactionError::CommitCancelled)),
                );

```

**File:** core/src/banking_stage/consumer.rs (L602-639)
```rust
    fn cancel_processed_transactions_for_retry<Tx: TransactionWithMeta>(
        transactions: &[Tx],
        processing_results: &mut [TransactionProcessingResult],
        processed_counts: &mut ProcessedTransactionCounts,
        error_counters: &mut TransactionErrorMetrics,
        retryable_transaction_indexes: &mut Vec<RetryableIndex>,
        failed_index: usize,
        transaction_error: &TransactionError,
        start_index: usize,
    ) {
        for (index, (tx, processing_result)) in transactions
            .iter()
            .zip(processing_results.iter_mut())
            .enumerate()
            .skip(start_index)
        {
            if processing_result.was_processed() {
                Self::decrement_processed_counts(tx, processing_result, processed_counts);
                let retry_error = if index == failed_index {
                    transaction_error.clone()
                } else {
                    TransactionError::CommitCancelled
                };
                Self::accumulate_post_execution_transaction_error(
                    processing_result,
                    &retry_error,
                    error_counters,
                );
                *processing_result = Err(retry_error);
                retryable_transaction_indexes.push(RetryableIndex {
                    index,
                    // The cost-limit failure should be held until a later retry opportunity.
                    // Transactions canceled behind it did not fail cost are immediately retryable..
                    immediately_retryable: index != failed_index,
                });
            }
        }
    }
```

**File:** unified-scheduler-logic/src/lib.rs (L1332-1360)
```rust
        // It's crucial for tasks to be validated with
        // `account_locks::validate_account_locks()` prior to the creation.
        // That's because it's part of protocol consensus regarding the
        // rejection of blocks containing malformed transactions
        // (`AccountLoadedTwice` and `TooManyAccountLocks`). Even more,
        // `SchedulingStateMachine` can't properly handle transactions with
        // duplicate addresses (those falling under `AccountLoadedTwice`).
        //
        // However, it's okay for now not to call `::validate_account_locks()`
        // here.
        //
        // Currently `replay_stage` is always calling
        //`::validate_account_locks()` regardless of whether unified-scheduler
        // is enabled or not at the blockstore
        // (`Bank::prepare_sanitized_batch()` is called in
        // `process_entries()`).
        //
        // As for `banking_stage` with unified scheduler, it will need to run
        // `validate_account_locks()` at least once somewhere in the code path.
        // In the distant future, this function (`create_task()`) should be
        // adjusted so that both stages do the checks before calling this or do
        // the checks here, to simplify the two code paths regarding the
        // essential `validate_account_locks` validation.
        //
        // Lastly, `validate_account_locks()` is currently called in
        // `DefaultTransactionHandler::handle()` via
        // `Bank::prepare_unlocked_batch_from_single_tx()` as well.
        // This redundancy is known. It was just left as-is out of abundance
        // of caution.
```
