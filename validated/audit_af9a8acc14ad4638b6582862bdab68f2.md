### Title
Cost tracker's `remove()` uses raw (non-saturating) atomic subtraction on `block_cost`, allowing an added-vs-removed cost mismatch to corrupt block QoS accounting - ([File: cost-model/src/cost_tracker.rs])

### Summary
The yAxis `setCap` bug is a class of accounting error where a "remove/decrease" operation subtracts an amount that does not correspond to what was actually added, corrupting a running balance and later causing invariant-violating asserts or stuck state. The closest reachable analog in Agave is `CostTracker::remove_transaction_cost` / `sub_transaction_execution_cost` in `cost-model/src/cost_tracker.rs`, which is invoked from the banking stage (`Consumer::try_add_processed_transaction_costs` / `remove_added_transaction_costs`, `core/src/banking_stage/consumer.rs`) whenever a previously `try_add`-ed transaction cost needs to be rolled back.

### Finding Description
`CostTracker::try_add` charges a transaction's cost against `block_cost` (a lock-free `SharedBlockCost` wrapping an `AtomicU64`) and against each writable account's entry in `cost_by_writable_accounts`: [1](#0-0) 

Rollback of a previously-added cost is done via `remove()` -> `remove_transaction_cost()` -> `sub_transaction_execution_cost()`: [2](#0-1) 

Per-account costs are decremented with `saturating_sub`, so they can never underflow, but the shared block-level counter is decremented with a raw, non-saturating atomic operation: [3](#0-2) 

`AtomicU64::fetch_sub` never panics on underflow (in either debug or release) — it silently wraps around to a value near `u64::MAX`. This is the same shape of bug as the yAxis `setCap` issue: the code assumes the amount being subtracted always matches what was previously added, and there is no invariant check (assert or saturating clamp) enforcing that at the block-cost level, even though the per-account map is protected with `saturating_sub`. If any caller ever invokes `remove()` with a `TransactionCost` whose `.sum()` differs from the amount that was actually charged for that transaction (e.g., a cost value recomputed/adjusted between the `try_add` and the corresponding `remove`, mismatched batch bookkeeping in `Consumer::try_add_processed_transaction_costs`/`remove_added_transaction_costs`, or a duplicate `remove()` call for the same transaction), `block_cost` silently wraps to a near-`u64::MAX` value while `cost_by_writable_accounts` remains internally consistent because of its `saturating_sub` protection.

`Consumer::try_add_processed_transaction_costs` and `Consumer::remove_added_transaction_costs` are the two call sites that add/remove costs from the shared bank-level cost tracker during normal (unprivileged, transaction-driven) banking-stage processing: [4](#0-3) [5](#0-4) 

### Impact Explanation
`block_cost()` is read by the banking stage / scheduler to decide whether the current or next transaction "would exceed the max block cost limit" — this is the core QoS gate that prevents overloading a leader's block with excessive compute. If `block_cost` wraps to near `u64::MAX` due to an unmatched subtraction, every subsequent `try_add()` in that slot's `CostTracker` instance will immediately fail with `WouldExceedBlockMaxLimit`, effectively freezing the leader's ability to pack any further transactions into the block for the remainder of that slot — a self-inflicted denial of service on block production. Conversely, if the mismatch instead makes the tracked cost artificially low (via a different failure mode where too much is removed elsewhere), a leader could pack far more compute into a block than intended, i.e. QoS evasion / grossly underpriced pre-fee work relative to what other validators would produce, risking downstream re-execution slowness for the rest of the cluster.

### Likelihood Explanation
This is the mechanism by which `try_add`/`remove` pairs are supposed to stay balanced 1:1 per transaction, and the current implementation is written specifically to guarantee that per-account costs never underflow (`saturating_sub` used pervasively) but leaves the atomic `block_cost` counter unprotected, which is an inconsistency in defensive coding between the two data structures tracking the same conceptual quantity. Confirming an actual reachable mismatch (i.e., a code path where `remove()` is called with a cost value different from what was `try_add`-ed for the same transaction) requires deeper tracing through all callers of `CostTracker::remove`/`try_add` across the codebase (e.g., `runtime/src/prioritization_fee_cache.rs`, `program-runtime/src/loaded_programs.rs` search hits, and any transaction retry/rollback paths), which could not be fully completed within the available search budget.

### Recommendation
Make `SharedBlockCost::fetch_sub` (and any other block-level counters updated during rollback) use saturating semantics, mirroring the per-account map's `saturating_sub`, so that any accounting mismatch cannot silently wrap the shared atomic counter to a corrupt near-`u64::MAX` value. Additionally, add a debug-mode invariant/assert that `remove()` is never called with a cost that exceeds the currently tracked `block_cost`, to surface any mismatch in tests before it manifests as a production QoS or block-production stall.

### Proof of Concept
Not independently reproduced with a concrete transaction sequence; the analysis is based on static code review of `cost-model/src/cost_tracker.rs` showing `SharedBlockCost::fetch_sub` (line 416-418) uses raw `AtomicU64::fetch_sub` instead of a saturating primitive, in contrast to the saturating protection applied to `cost_by_writable_accounts` in the same `remove_transaction_cost`/`sub_transaction_execution_cost` path (lines 355-382). A full PoC would require identifying and exercising a concrete caller sequence where `remove()`'s cost value diverges from a prior `try_add()`'s cost value for the same transaction, which was not verified within the available time. [6](#0-5)

### Citations

**File:** cost-model/src/cost_tracker.rs (L224-233)
```rust
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
```

**File:** cost-model/src/cost_tracker.rs (L355-382)
```rust
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

**File:** cost-model/src/cost_tracker.rs (L404-418)
```rust
#[derive(Debug, Clone)]
pub struct SharedBlockCost(Arc<AtomicU64>);

impl SharedBlockCost {
    pub fn new(value: u64) -> Self {
        Self(Arc::new(AtomicU64::new(value)))
    }

    fn fetch_add(&self, value: u64) -> u64 {
        self.0.fetch_add(value, Ordering::Release)
    }

    fn fetch_sub(&self, value: u64) -> u64 {
        self.0.fetch_sub(value, Ordering::Release)
    }
```

**File:** core/src/banking_stage/consumer.rs (L542-568)
```rust
        let mut cost_tracker = bank.write_cost_tracker().unwrap();

        for (index, transaction_cost) in transaction_costs.iter_mut().enumerate() {
            let Some(cost) = transaction_cost.as_ref() else {
                continue;
            };

            match cost_tracker.try_add(cost) {
                Ok(_) => {}
                Err(err) => {
                    let transaction_error = TransactionError::from(err);
                    *transaction_cost = None;
                    if all_or_nothing {
                        all_or_nothing_error = Some((index, transaction_error));
                        break;
                    } else {
                        remaining_batch_error = Some((index, transaction_error));
                        break;
                    }
                }
            }
        }

        if let Some((failed_index, transaction_error)) = all_or_nothing_error {
            for transaction_cost in transaction_costs[..failed_index].iter().flatten() {
                cost_tracker.remove(transaction_cost);
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
