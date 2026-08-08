### Title
Non-saturating subtraction in `CostTracker::remove_transaction_cost` can permanently corrupt block-cost accounting on a double-remove/rollback path - (File: cost-model/src/cost_tracker.rs)

### Summary
`CostTracker::remove_transaction_cost` decrements `allocated_accounts_data_size`, `transaction_count`, `transaction_signature_count`, `secp256k1_instruction_signature_count`, and `ed25519_instruction_signature_count` with plain `-=` operators instead of `saturating_sub`, unlike the sibling function `sub_transaction_execution_cost`, which is careful to use `saturating_sub` for `cost_by_writable_accounts` entries.

### Finding Description
`CostTracker::add_transaction_cost` and `CostTracker::remove_transaction_cost` are meant to be called in matched pairs whenever a transaction's cost is added to, or removed from, the in-flight block-cost bookkeeping used by `banking_stage`'s QoS/cost-limiting logic. [1](#0-0) 

`remove_transaction_cost` uses raw `-=` for `allocated_accounts_data_size`, `transaction_count`, and the signature counters, while the account-cost decrement path (`sub_transaction_execution_cost`) explicitly guards against underflow with `saturating_sub`: [2](#0-1) 

`remove_transaction_cost` (and the block-level `remove`) are invoked from `banking_stage`'s consumer whenever transactions that were previously admitted via `cost_tracker.try_add()` must be rolled back — e.g. on `all_or_nothing` batch failures or when `record_transactions` fails after costs were already added: [3](#0-2) [4](#0-3) [5](#0-4) 

This mirrors the root cause of the reported Ethos bug: code assumes a tracked "credit" (`currentAllocated`/`transaction_count`) can never exceed the corresponding "actual" value, and performs an unchecked subtraction on that assumption. If any code path calls `remove_transaction_cost` for a cost that was already removed, or removes more than what was actually recorded for that entry (e.g. a batch/rollback bookkeeping mismatch between `try_add_processed_transaction_costs`'s all-or-nothing rollback and a subsequent `remove_added_transaction_costs` call on the same transaction set, or any future refactor of these call sites), `transaction_count -= 1` or `allocated_accounts_data_size -= size` underflow. Since these are plain integer subtractions (not `checked_sub`/`saturating_sub`), and the crate does not appear to force `overflow-checks = true`, this silently wraps in release builds to a value near `u64::MAX` rather than panicking.

### Impact Explanation
A wrapped `transaction_count` or `allocated_accounts_data_size` corrupts the `CostTracker` state persistently within that `Bank`. Because `would_fit`/`try_add` compare these accumulated values against configured block/account limits (`self.limits.allocated_data_size`, etc.), a wrapped-to-near-`u64::MAX` value would cause the cost tracker to reject essentially all subsequent transactions in the block/slot as exceeding limits — effectively halting block production/inclusion for that bank, analogous to the "Active Pool will stop working" denial-of-service described in the source report. This is a concrete, high-severity liveness/DoS class impact (QoS evasion or block production stall) if the accounting invariant `add`/`remove` pairing is ever violated.

### Likelihood Explanation
This is contingent on a currently-unproven mismatch between add/remove call pairing at the `banking_stage::consumer` layer (e.g., a future refactor or edge case that removes a cost twice, or removes a cost that was never fully added, such as partial success in `try_add_processed_transaction_costs`'s all-or-nothing rollback interacting with `remove_added_transaction_costs`). I could not, within available tooling, fully trace every call site to prove a concrete double-removal trigger exists in the current code as written; the finding is that the underlying arithmetic has no defensive underflow protection (unlike the neighboring function `sub_transaction_execution_cost`), so any bookkeeping bug in the caller becomes silent, non-panicking state corruption rather than a fail-fast panic. This inconsistency (defensive `saturating_sub` in one function vs. raw `-=` in the sibling function operating on the same struct) is itself a code-quality/robustness gap that should be fixed defensively regardless of whether a live trigger exists today.

### Recommendation
Change `remove_transaction_cost` in `cost-model/src/cost_tracker.rs` to use `saturating_sub` (or `checked_sub` with a `debug_assert`/metric on underflow) for `allocated_accounts_data_size`, `transaction_count`, `transaction_signature_count`, `secp256k1_instruction_signature_count`, and `ed25519_instruction_signature_count`, matching the defensive style already used in `sub_transaction_execution_cost`. Additionally, add invariant checks/tests asserting that `add_transaction_cost`/`remove_transaction_cost` are always called in balanced pairs, and add metrics/logging when an underflow would have occurred so operators can detect any latent caller-side accounting bug.

### Proof of Concept
Not independently reproduced against a live agave build within this analysis; the finding is derived from static code inspection of `cost-model/src/cost_tracker.rs` (lines 312-373) contrasted with the reachable call sites in `core/src/banking_stage/consumer.rs` (lines 397-404, 565-581, 654-662) that invoke `remove`/`remove_transaction_cost` on rollback paths. A concrete runtime PoC would require constructing a `banking_stage` test that forces a double-removal (e.g., overlapping all-or-nothing rollback with a subsequent record-failure rollback for the same transaction) and observing `cost_tracker.transaction_count()` wrap to a near-`u64::MAX` value in a release (`overflow-checks = false`) build.

### Citations

**File:** cost-model/src/cost_tracker.rs (L312-336)
```rust
    // Returns the highest account cost for all write-lock accounts `TransactionCost` updated
    fn add_transaction_cost(&mut self, tx_cost: &TransactionCost<impl TransactionWithMeta>) -> u64 {
        self.allocated_accounts_data_size += tx_cost.allocated_accounts_data_size();
        self.transaction_count += 1;
        self.transaction_signature_count += tx_cost.num_transaction_signatures();
        self.secp256k1_instruction_signature_count +=
            tx_cost.num_secp256k1_instruction_signatures();
        self.ed25519_instruction_signature_count += tx_cost.num_ed25519_instruction_signatures();
        self.secp256r1_instruction_signature_count +=
            tx_cost.num_secp256r1_instruction_signatures();
        self.add_transaction_execution_cost(tx_cost, tx_cost.sum())
    }

    fn remove_transaction_cost(&mut self, tx_cost: &TransactionCost<impl TransactionWithMeta>) {
        let cost = tx_cost.sum();
        self.sub_transaction_execution_cost(tx_cost, cost);
        self.allocated_accounts_data_size -= tx_cost.allocated_accounts_data_size();
        self.transaction_count -= 1;
        self.transaction_signature_count -= tx_cost.num_transaction_signatures();
        self.secp256k1_instruction_signature_count -=
            tx_cost.num_secp256k1_instruction_signatures();
        self.ed25519_instruction_signature_count -= tx_cost.num_ed25519_instruction_signatures();
        self.secp256r1_instruction_signature_count -=
            tx_cost.num_secp256r1_instruction_signatures();
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

**File:** core/src/banking_stage/consumer.rs (L397-404)
```rust
        if let Err(recorder_err) = recording_result {
            Self::remove_added_transaction_costs(bank, &transaction_costs);

            Self::extend_processed_retryable_transaction_indexes(
                &mut retryable_transaction_indexes,
                &processing_results,
            );

```

**File:** core/src/banking_stage/consumer.rs (L565-581)
```rust
        if let Some((failed_index, transaction_error)) = all_or_nothing_error {
            for transaction_cost in transaction_costs[..failed_index].iter().flatten() {
                cost_tracker.remove(transaction_cost);
            }
            transaction_costs.iter_mut().for_each(|cost| *cost = None);
            retryable_transaction_indexes.clear();

            Self::cancel_processed_transactions_for_retry(
                transactions,
                processing_results,
                processed_counts,
                error_counters,
                &mut retryable_transaction_indexes,
                failed_index,
                &transaction_error,
                0,
            );
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
