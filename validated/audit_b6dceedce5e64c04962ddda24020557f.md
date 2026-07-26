Let me look at the key remaining pieces — how `CommittedOutput` relates to `TransactionOutput` in the hook call chain, and whether aborted transactions actually reach `on_transaction_committed`.

Now I have the complete picture. Let me synthesize all findings.

**Critical type chain established:**
- `executor.rs` line 75: `type CommittedOutput<E> = <<E as ExecutorTask>::Output as TransactionOutput>::CommittedOutput;`
- `mod.rs` line 361: `type CommittedOutput = TransactionOutput;` (for `AptosTransactionOutput`)
- `output.rs` line 37: `impl CommittedTransactionOutput for TransactionOutput`

So `CommittedOutput<E>` = `TransactionOutput` in the Aptos VM context. The hook type matches exactly.

**Move-level abort vs fatal abort:** In sequential execution (executor.rs lines 2186–2409), `ExecutionStatus::Abort` is a **fatal VM error** that returns `Err` immediately — it never reaches the hook. A Move-level abort (gas charged, transaction kept) produces `ExecutionStatus::Success(output)` with `TransactionStatus::Keep(ExecutionStatus::MoveAbort(...))`. The hook IS called for this case. Same in parallel execution via `record_finalized_output`.

**The bug is real and the path is complete:**

---

### Title
Cross-Shard Deadlock via Move-Aborted Transaction Silently Dropping Dependent Write Notification — (`aptos-move/aptos-vm/src/sharded_block_executor/cross_shard_client.rs`)

### Summary
`CrossShardCommitSender::on_transaction_committed` unconditionally calls `send_remote_update_for_success` for every committed transaction that has dependent edges, regardless of whether the transaction succeeded or aborted at the Move level. `send_remote_update_for_success` only iterates over the **actual write set** of the committed output. When a transaction aborts (Move abort, out-of-gas, etc.) and its committed write set contains only gas-charge keys — not the cross-shard key K — no `RemoteTxnWriteMsg` is ever sent for K. The dependent shard's `RemoteStateValue` for K remains permanently in `Waiting` state, and any thread calling `CrossShardStateView::get_state_value` for K blocks forever on the condvar, hanging the entire block execution.

### Finding Description

**Step 1 — Dependent edge registration.**
`CrossShardCommitSender::new` builds `dependent_edges` from `sub_block.txn_with_index_iter()` using `cross_shard_dependencies.dependent_edges()`. This is a static, pre-execution map derived from the partitioner's declared write sets. [1](#0-0) 

**Step 2 — Dependent shard registers K as `Waiting`.**
`CrossShardStateView::create_cross_shard_state_view` collects all `required_edges` keys and inserts each as `RemoteStateValue::waiting()` into `cross_shard_data`. [2](#0-1) [3](#0-2) 

**Step 3 — Hook fires for Move-aborted transactions.**
`record_finalized_output` calls `hook.on_transaction_committed` with the materialized `CommittedOutput<E>` = `TransactionOutput`. A Move-level abort produces `ExecutionStatus::Success(output)` (not `ExecutionStatus::Abort`, which is a fatal VM error), so the hook is reached. The committed `TransactionOutput` for a Move-aborted transaction contains only gas-charge writes, not K. [4](#0-3) [5](#0-4) [6](#0-5) 

**Step 4 — `send_remote_update_for_success` silently skips K.**
The function iterates only over `txn_output.write_set().expect_write_op_iter()`. If K is absent from the write set (because the transaction aborted before writing it), the inner `edges.get(state_key)` branch for K is never entered, and no `RemoteTxnWriteMsg` is sent to shard-1. [7](#0-6) 

**Step 5 — Dependent shard blocks forever.**
`CrossShardStateView::get_state_value` finds K in `cross_shard_data` and calls `value.get_value()`. `RemoteStateValue::get_value()` spins on a `Condvar::wait` loop that only exits when `RemoteValueStatus::Ready` is set. Since no message ever arrives, the condvar never fires. [8](#0-7) [9](#0-8) 

There is **no fallback**: once K is found in `cross_shard_data`, the base view is never consulted. There is **no timeout** on the condvar. There is **no abort notification path** in `TransactionCommitHook` — the trait has only `on_transaction_committed`. [10](#0-9) 

### Impact Explanation
The dependent shard's executor thread blocks indefinitely inside `get_state_value`. The `rayon::scope` in `execute_transactions_with_dependencies` never completes. The `block_on(callback_receiver)` call in the coordinator never returns. The entire block execution for that round hangs permanently, causing a **material chain availability failure** — no further blocks can be produced on the affected validator until the process is restarted. [11](#0-10) 

### Likelihood Explanation
The attacker needs two conditions:
1. The partitioner assigns a cross-shard dependent edge for key K to the attacker's transaction (shard-0 → shard-1). This happens when the static analyzer declares K as a write of the transaction. An attacker can craft a transaction that calls a function known to write K, causing the analyzer to declare K as a write.
2. The transaction aborts before writing K at runtime (e.g., a Move `abort` or assertion failure before the write instruction executes).

Both conditions are achievable by an unprivileged user submitting a crafted transaction. The attacker does not need any privileged keys, validator access, or governance power.

### Recommendation
In `send_remote_update_for_success` (or in `on_transaction_committed`), after iterating over the actual write set, check which registered dependent keys were **not** covered and send a `RemoteTxnWriteMsg` with `write_op = None` for each uncovered key. This signals the dependent shard to fall back to the base view for K, unblocking the condvar.

```rust
fn send_remote_update_for_success(&self, txn_idx: TxnIndex, txn_output: &TransactionOutput) {
    let edges = self.dependent_edges.get(&txn_idx).unwrap();
    let mut sent_keys = HashSet::new();

    for (state_key, write_op) in txn_output.write_set().expect_write_op_iter() {
        if let Some(dependent_shard_ids) = edges.get(state_key) {
            sent_keys.insert(state_key.clone());
            for (dependent_shard_id, round_id) in dependent_shard_ids.iter() {
                // ... send Some(write_op) message as before ...
            }
        }
    }

    // Send None for any dependent key not present in the write set (e.g., aborted txn)
    for (state_key, dependent_shard_ids) in edges.iter() {
        if !sent_keys.contains(state_key) {
            for (dependent_shard_id, round_id) in dependent_shard_ids.iter() {
                let message = RemoteTxnWriteMsg(RemoteTxnWrite::new(state_key.clone(), None));
                // ... send message ...
            }
        }
    }
}
```

### Proof of Concept

```rust
// Two-shard block: shard-0 txn T declares write of key K (cross-shard dep to shard-1),
// but T aborts (Move abort) before writing K.
// Shard-1 txn reads K via CrossShardStateView.
//
// Expected (correct): shard-1 receives RemoteTxnWriteMsg(K, None), reads K from base view.
// Actual (buggy):     shard-1 blocks forever in RemoteStateValue::get_value() condvar.

#[test]
fn test_cross_shard_deadlock_on_abort() {
    // 1. Build AnalyzedTransaction for shard-0 that declares write of K
    //    but whose Move execution aborts before writing K.
    // 2. Build AnalyzedTransaction for shard-1 that reads K (required_edge from shard-0).
    // 3. Partition into two shards with the cross-shard edge K: shard-0 -> shard-1.
    // 4. Execute the two-shard block.
    // 5. Assert: shard-1's CrossShardStateView::get_state_value(&K) returns within timeout.
    //    (This assertion will FAIL / timeout under the current code.)
    let result = std::thread::spawn(|| {
        // ... execute shard-1 block ...
        cross_shard_state_view.get_state_value(&K)
    });
    assert!(result.join_timeout(Duration::from_secs(5)).is_ok(),
        "Shard-1 blocked forever waiting for K — deadlock confirmed");
}
``` [12](#0-11) [13](#0-12)

### Citations

**File:** aptos-move/aptos-vm/src/sharded_block_executor/cross_shard_client.rs (L66-86)
```rust
        let mut dependent_edges = HashMap::new();
        let mut num_dependent_edges = 0;
        for (txn_idx, txn_with_deps) in sub_block.txn_with_index_iter() {
            let mut storage_locations_to_target = HashMap::new();
            for (txn_id_with_shard, storage_locations) in txn_with_deps
                .cross_shard_dependencies
                .dependent_edges()
                .iter()
            {
                for storage_location in storage_locations {
                    storage_locations_to_target
                        .entry(storage_location.clone().into_state_key())
                        .or_insert_with(HashSet::new)
                        .insert((txn_id_with_shard.shard_id, txn_id_with_shard.round_id));
                    num_dependent_edges += 1;
                }
            }
            if !storage_locations_to_target.is_empty() {
                dependent_edges.insert(txn_idx as TxnIndex, storage_locations_to_target);
            }
        }
```

**File:** aptos-move/aptos-vm/src/sharded_block_executor/cross_shard_client.rs (L102-125)
```rust
    fn send_remote_update_for_success(&self, txn_idx: TxnIndex, txn_output: &TransactionOutput) {
        let edges = self.dependent_edges.get(&txn_idx).unwrap();

        for (state_key, write_op) in txn_output.write_set().expect_write_op_iter() {
            if let Some(dependent_shard_ids) = edges.get(state_key) {
                for (dependent_shard_id, round_id) in dependent_shard_ids.iter() {
                    trace!("Sending remote update for success for shard id {:?} and txn_idx: {:?}, state_key: {:?}, dependent shard id: {:?}", self.shard_id, txn_idx, state_key, dependent_shard_id);
                    let message = RemoteTxnWriteMsg(RemoteTxnWrite::new(
                        state_key.clone(),
                        Some(write_op.clone()),
                    ));
                    if *round_id == GLOBAL_ROUND_ID {
                        self.cross_shard_client.send_global_msg(message);
                    } else {
                        self.cross_shard_client.send_cross_shard_msg(
                            *dependent_shard_id,
                            *round_id,
                            message,
                        );
                    }
                }
            }
        }
    }
```

**File:** aptos-move/aptos-vm/src/sharded_block_executor/cross_shard_client.rs (L128-135)
```rust
impl TransactionCommitHook<TransactionOutput> for CrossShardCommitSender {
    fn on_transaction_committed(&self, txn_idx: TxnIndex, txn_output: &TransactionOutput) {
        let global_txn_idx = txn_idx + self.index_offset;
        if self.dependent_edges.contains_key(&global_txn_idx) {
            self.send_remote_update_for_success(global_txn_idx, txn_output);
        }
    }
}
```

**File:** aptos-move/aptos-vm/src/sharded_block_executor/cross_shard_state_view.rs (L26-38)
```rust
    pub fn new(cross_shard_keys: HashSet<StateKey>, base_view: &'a S) -> Self {
        let mut cross_shard_data = HashMap::new();
        trace!(
            "Initializing cross shard state view with {} keys",
            cross_shard_keys.len(),
        );
        for key in cross_shard_keys {
            cross_shard_data.insert(key, RemoteStateValue::waiting());
        }
        Self {
            cross_shard_data,
            base_view,
        }
```

**File:** aptos-move/aptos-vm/src/sharded_block_executor/cross_shard_state_view.rs (L58-71)
```rust
    pub fn create_cross_shard_state_view(
        base_view: &'a S,
        transactions: &[TransactionWithDependencies<AnalyzedTransaction>],
    ) -> CrossShardStateView<'a, S> {
        let mut cross_shard_state_key = HashSet::new();
        for txn in transactions {
            for (_, storage_locations) in txn.cross_shard_dependencies.required_edges_iter() {
                for storage_location in storage_locations {
                    cross_shard_state_key.insert(storage_location.clone().into_state_key());
                }
            }
        }
        CrossShardStateView::new(cross_shard_state_key, base_view)
    }
```

**File:** aptos-move/aptos-vm/src/sharded_block_executor/cross_shard_state_view.rs (L77-82)
```rust
    fn get_state_value(&self, state_key: &StateKey) -> Result<Option<StateValue>, StateViewError> {
        if let Some(value) = self.cross_shard_data.get(state_key) {
            return Ok(value.get_value());
        }
        self.base_view.get_state_value(state_key)
    }
```

**File:** aptos-move/block-executor/src/executor.rs (L75-75)
```rust
type CommittedOutput<E> = <<E as ExecutorTask>::Output as TransactionOutput>::CommittedOutput;
```

**File:** aptos-move/block-executor/src/executor.rs (L1174-1176)
```rust
        if let Some(hook) = &self.transaction_commit_hook {
            hook.on_transaction_committed(txn_idx, &committed_output);
        }
```

**File:** aptos-move/aptos-vm/src/block_executor/mod.rs (L359-362)
```rust
impl BlockExecutorTransactionOutput for AptosTransactionOutput {
    type BeforeMaterializationGuard<'a> = BeforeMaterializationGuard<'a>;
    type CommittedOutput = TransactionOutput;
    type Txn = SignatureVerifiedTransaction;
```

**File:** aptos-move/aptos-vm/src/sharded_block_executor/remote_state_value.rs (L16-27)
```rust
    pub fn waiting() -> Self {
        Self {
            value_condition: Arc::new((Mutex::new(RemoteValueStatus::Waiting), Condvar::new())),
        }
    }

    pub fn set_value(&self, value: Option<StateValue>) {
        let (lock, cvar) = &*self.value_condition;
        let mut status = lock.lock().unwrap();
        *status = RemoteValueStatus::Ready(value);
        cvar.notify_all();
    }
```

**File:** aptos-move/aptos-vm/src/sharded_block_executor/remote_state_value.rs (L29-39)
```rust
    pub fn get_value(&self) -> Option<StateValue> {
        let (lock, cvar) = &*self.value_condition;
        let mut status = lock.lock().unwrap();
        while let RemoteValueStatus::Waiting = *status {
            status = cvar.wait(status).unwrap();
        }
        match &*status {
            RemoteValueStatus::Ready(value) => value.clone(),
            RemoteValueStatus::Waiting => unreachable!(),
        }
    }
```

**File:** aptos-move/block-executor/src/txn_commit_hook.rs (L8-10)
```rust
pub trait TransactionCommitHook<O>: Send + Sync {
    fn on_transaction_committed(&self, txn_idx: TxnIndex, output: &O);
}
```

**File:** aptos-move/aptos-vm/src/sharded_block_executor/sharded_executor_service.rs (L134-181)
```rust
        executor_thread_pool.clone().scope(|s| {
            s.spawn(move |_| {
                CrossShardCommitReceiver::start(
                    cross_shard_state_view_clone,
                    cross_shard_client,
                    round,
                );
            });
            s.spawn(move |_| {
                let txn_provider =
                    DefaultTxnProvider::new_without_info(signature_verified_transactions);
                let ret = AptosVMBlockExecutorWrapper::execute_block(
                    &txn_provider,
                    aggr_overridden_state_view.as_ref(),
                    // Since we execute blocks in parallel, we cannot share module caches, so each
                    // thread has its own caches.
                    &AptosModuleCacheManager::new(),
                    config,
                    TransactionSliceMetadata::unknown(),
                    cross_shard_commit_sender,
                )
                .map(BlockOutput::into_transaction_outputs_forced);
                if let Some(shard_id) = shard_id {
                    trace!(
                        "executed sub block for shard {} and round {}",
                        shard_id,
                        round
                    );
                    // Send a self message to stop the cross-shard commit receiver.
                    cross_shard_client_clone.send_cross_shard_msg(
                        shard_id,
                        round,
                        CrossShardMsg::StopMsg,
                    );
                } else {
                    trace!("executed block for global shard and round {}", round);
                    // Send a self message to stop the cross-shard commit receiver.
                    cross_shard_client_clone.send_global_msg(CrossShardMsg::StopMsg);
                }
                callback.send(ret).unwrap();
                executor_thread_pool_clone.spawn(move || {
                    // Explicit async drop
                    drop(txn_provider);
                });
            });
        });

        block_on(callback_receiver).unwrap()
```
