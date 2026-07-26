### Title
Missing Cross-Shard Abort Notification Causes Permanent Executor Deadlock — (`aptos-move/aptos-vm/src/sharded_block_executor/cross_shard_client.rs`)

---

### Summary

When a source-shard transaction that has partitioner-assigned `dependent_edges` aborts at runtime (Move abort, gas exhaustion, etc.), `CrossShardCommitSender` never sends a `RemoteTxnWriteMsg` for the cross-shard state key. The dependent shard's `RemoteStateValue` remains permanently in `RemoteValueStatus::Waiting`, causing `RemoteStateValue::get_value()` to block indefinitely on a condvar with no timeout. This deadlocks the dependent shard's executor thread for the remainder of the block — and because `StopMsg` is only sent after `execute_block` returns, the `CrossShardCommitReceiver` also never terminates. The shard is permanently wedged for that block.

---

### Finding Description

**The question's premise is partially incorrect but the vulnerability is real.**

The question claims `on_transaction_committed` is "never called for aborted txns." This is wrong. `record_finalized_output` — which calls `on_transaction_committed` — is invoked for every finalized transaction, including Move-aborted and gas-exhausted ones, via `drain_commit_queue` / `TaskKind::PostCommitProcessing`: [1](#0-0) [2](#0-1) 

The real bug is in `send_remote_update_for_success`, which is the only code path that notifies dependent shards. It iterates only over the **actual write set** of the committed output: [3](#0-2) 

For an aborted transaction, the write set contains only gas-charge writes to the sender's account — never the cross-shard state key `K` that the partitioner declared as a `dependent_edge`. So the inner `if let Some(dependent_shard_ids) = edges.get(state_key)` branch is never entered for `K`, and no `RemoteTxnWriteMsg` is sent.

The `RemoteTxnWrite` struct's own comment acknowledges the design intent to handle this case (`write_op: None` for aborted transactions), but no code path ever constructs or sends such a message: [4](#0-3) 

On the dependent shard, `CrossShardStateView::create_cross_shard_state_view` registers every `required_edges` key as `RemoteStateValue::waiting()`: [5](#0-4) 

When the dependent transaction calls `get_state_value(K)`, it hits `RemoteStateValue::get_value()`, which spins on a condvar with **no timeout**: [6](#0-5) 

Because `execute_block` never returns, the `StopMsg` that would terminate `CrossShardCommitReceiver` is never sent: [7](#0-6) 

The entire `executor_thread_pool.scope(...)` call blocks forever, wedging the shard. [8](#0-7) 

---

### Impact Explanation

The dependent shard's executor thread is permanently blocked for the affected block. All transactions in that shard's sub-block never complete. Because `execute_sub_block` never returns, `execute_block` never returns, and the shard's `start()` loop is permanently stuck — the shard cannot process any subsequent block. All user balances whose transactions were assigned to that shard's sub-block are effectively frozen until the shard process is restarted. An attacker who can trigger this on every block renders the shard permanently unavailable.

---

### Likelihood Explanation

An unprivileged attacker submits a transaction that:
1. Declares a write to a state key `K` that another transaction in the same block reads (causing the partitioner to assign a cross-shard `dependent_edge` from the attacker's transaction to the victim's).
2. Aborts at runtime via a Move `abort` code or by exhausting gas.

The partitioner operates on declared access patterns, not runtime outcomes, so it cannot prevent this. The attacker does not need privileged keys, validator access, or governance powers.

---

### Recommendation

Add a `send_remote_update_for_abort` path in `CrossShardCommitSender::on_transaction_committed`. When the committed output's status is an abort (empty or gas-only write set), iterate over all `dependent_edges` for that transaction and send a `RemoteTxnWriteMsg` with `write_op: None` for each cross-shard state key. The `CrossShardCommitReceiver` already handles `write_op: None` correctly via `write_op.and_then(|w| w.as_state_value())`, which resolves the `RemoteStateValue` to `Ready(None)` — unblocking the dependent shard. [9](#0-8) 

Additionally, add a timeout to `RemoteStateValue::get_value()` as a defense-in-depth measure.

---

### Proof of Concept

```
1. Partition a block of two transactions [T1, T2] across two shards:
   - T1 (shard A): writes state_key K, but calls abort(0) in Move body.
   - T2 (shard B): reads state_key K (required_edge from shard A).

2. Shard B's CrossShardStateView registers K as RemoteValueStatus::Waiting.

3. Shard A executes T1; BlockSTM commits it (with gas-only write set).
   on_transaction_committed is called; send_remote_update_for_success
   iterates T1's write set — K is absent — no message sent to shard B.

4. Shard B's T2 calls get_state_value(K) → get_value() → cvar.wait() forever.

5. execute_block on shard B never returns; StopMsg never sent;
   CrossShardCommitReceiver never terminates.

6. Assert (via timeout): shard B's CrossShardStateView::get_state_value
   for K does not return within N seconds → deadlock confirmed.
```

### Citations

**File:** aptos-move/block-executor/src/executor.rs (L1174-1176)
```rust
        if let Some(hook) = &self.transaction_commit_hook {
            hook.on_transaction_committed(txn_idx, &committed_output);
        }
```

**File:** aptos-move/block-executor/src/executor.rs (L1207-1216)
```rust
        let drain_commit_queue = || -> Result<(), PanicError> {
            while let Ok(txn_idx) = scheduler.pop_from_commit_queue() {
                let output = self.materialize_txn_commit(
                    txn_idx,
                    scheduler_wrapper,
                    environment,
                    shared_sync_params,
                )?;
                self.record_finalized_output(txn_idx, txn_idx, output, shared_sync_params)?;
            }
```

**File:** aptos-move/aptos-vm/src/sharded_block_executor/cross_shard_client.rs (L33-37)
```rust
                RemoteTxnWriteMsg(txn_commit_msg) => {
                    let (state_key, write_op) = txn_commit_msg.take();
                    cross_shard_state_view
                        .set_value(&state_key, write_op.and_then(|w| w.as_state_value()));
                },
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

**File:** aptos-move/aptos-vm/src/sharded_block_executor/messages.rs (L14-18)
```rust
pub struct RemoteTxnWrite {
    state_key: StateKey,
    // The write op is None if the transaction is aborted.
    write_op: Option<WriteOp>,
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
