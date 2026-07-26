The code trace is complete. Here is the definitive assessment.

---

## Vulnerability Analysis

### Core Code Path

**Step 1 – Partitioner assigns static cross-shard edges.**
`take_txn_with_dep` in `state.rs` builds `required_edges` and `dependent_edges` from the transaction's statically-analyzed `write_sets` / `read_sets`, with no regard for whether the transaction will succeed at runtime. [1](#0-0) 

**Step 2 – Dependent shard initializes all required keys as `Waiting`.**
`CrossShardStateView::create_cross_shard_state_view` inserts every key from `required_edges_iter()` as `RemoteStateValue::waiting()`. [2](#0-1) 

**Step 3 – `get_value()` blocks indefinitely with no timeout.**
`RemoteStateValue::get_value()` spins on `cvar.wait()` until `set_value` is called. There is no timeout, no cancellation path. [3](#0-2) 

**Step 4 – `on_transaction_committed` is called for ALL committed transactions, including Move-aborted ones.**
`record_finalized_output` calls the hook unconditionally for every finalized output, whether the transaction succeeded or aborted. [4](#0-3) 

**Step 5 – `send_remote_update_for_success` only iterates the actual write set.**
It sends a `RemoteTxnWrite` only for keys that appear in `txn_output.write_set()`. For a Move-aborted transaction, the write set contains only the gas-deduction write (sender's account resource), not the application-level writes. [5](#0-4) 

**Step 6 – No abort-path message exists.**
A `grep` for `send_remote_update_for_abort`, `on_transaction_aborted`, or any equivalent returns zero results. There is no code path that sends a `RemoteTxnWrite` (with `None`) for keys that were in `dependent_edges` but absent from the aborted transaction's write set. [6](#0-5) 

### The Gap

When txn A aborts, `on_transaction_committed` fires, `send_remote_update_for_success` iterates txn A's write set (gas deduction only), finds no match for key K in `dependent_edges`, sends nothing, and returns. The dependent shard's `CrossShardStateView` entry for K remains `RemoteValueStatus::Waiting` forever. Any dependent transaction that reads K calls `get_value()` and blocks on `cvar.wait()` with no escape.

### Attacker Reachability

An unprivileged user submits a transaction that:
- Statically touches key K (so the partitioner assigns it as the writer and creates the cross-shard edge), and
- Aborts at runtime (Move abort, insufficient gas, any `ExecutionFailure`).

Both conditions are trivially achievable with a normal user transaction. No validator control, governance power, or privileged key is required.

### Existing Guards Checked

- `on_transaction_committed` does not check `txn_output.status()` before calling `send_remote_update_for_success`. No guard.
- `send_remote_update_for_success` does not iterate `dependent_edges` to send `None` for unwritten keys. No guard.
- `RemoteStateValue::get_value()` has no timeout. No guard.

### Scope / Deployment Caveat

The sharded block executor lives in `aptos-move/aptos-vm/src/sharded_block_executor/` — production code, not in `experimental/`. However, sharded execution requires explicit validator-side configuration to activate. Whether it is currently enabled on mainnet is not determinable from the repository alone. If it is not active on mainnet today, the practical impact is deferred to whenever it is enabled.

---

### Title
`CrossShardCommitSender` silently drops cross-shard notifications for aborted transactions, causing permanent deadlock in dependent shards — (`aptos-move/aptos-vm/src/sharded_block_executor/cross_shard_client.rs`)

### Summary
`send_remote_update_for_success` iterates only the committed write set. When a writer transaction aborts, keys in `dependent_edges` that were not written receive no `RemoteTxnWrite` message. The dependent shard's `CrossShardStateView` blocks indefinitely on `RemoteStateValue::get_value()`, deadlocking block execution.

### Finding Description
The `CrossShardCommitSender` is registered as a `TransactionCommitHook` and is invoked for every committed transaction, including those that abort at the Move level. Its sole notification method, `send_remote_update_for_success`, iterates `txn_output.write_set()` and sends a `RemoteTxnWrite` only for keys that appear in that write set. For an aborted transaction the write set contains only the gas-deduction write; application-level writes are absent. If any of those absent keys appear in `dependent_edges` (i.e., a later transaction on another shard declared a `required_edge` on that key), no message is ever sent. The dependent shard's `CrossShardStateView` initialized that key as `RemoteStateValue::waiting()`, and `get_value()` blocks on a `Condvar` with no timeout until `set_value` is called — which never happens.

### Impact Explanation
The dependent shard's execution thread deadlocks permanently. Because sharded execution proceeds round-by-round with each shard waiting for cross-shard values before it can commit its sub-block, a single stuck key freezes the entire block. No further blocks can be produced on the affected validator, constituting a material chain availability failure.

### Likelihood Explanation
Any user transaction that (a) touches a cross-shard key in its static read/write set and (b) aborts at runtime triggers the bug. Move aborts and out-of-gas conditions are routine. The attacker needs no special privileges and can craft such a transaction deliberately or encounter it accidentally.

### Recommendation
In `on_transaction_committed` (or in `send_remote_update_for_success`), after iterating the write set, iterate `dependent_edges` for the committed transaction and send a `RemoteTxnWrite` with `write_op = None` for every key in `dependent_edges` that was **not** present in the write set. This mirrors the existing `RemoteTxnWrite::new(state_key, None)` design already supported by the message type. [7](#0-6) 

### Proof of Concept
1. Configure two shards. Place txn A (writer of key K) on shard 0, txn B (reader of key K, `required_edge` on K from A) on shard 1.
2. Craft txn A so that its static write set includes K but its Move body aborts (e.g., `abort 1`).
3. Run sharded execution. Observe that `CrossShardCommitSender::on_transaction_committed` fires for txn A, `send_remote_update_for_success` finds K absent from the write set, sends nothing.
4. Assert that `CrossShardStateView::set_value` is never called for K on shard 1.
5. Observe that shard 1's execution thread blocks indefinitely on `RemoteStateValue::get_value()` for K, and the block never completes.

### Citations

**File:** execution/block-partitioner/src/v2/state.rs (L291-321)
```rust
    pub(crate) fn take_txn_with_dep(
        &self,
        round_id: RoundId,
        shard_id: ShardId,
        txn_idx: PrePartitionedTxnIdx,
    ) -> TransactionWithDependencies<AnalyzedTransaction> {
        let ori_txn_idx = self.ori_idxs_by_pre_partitioned[txn_idx];
        let txn = self.txns[ori_txn_idx].write().unwrap().take().unwrap();
        let mut deps = CrossShardDependencies::default();

        // Build required edges.
        let write_set = self.write_sets[ori_txn_idx].read().unwrap();
        let read_set = self.read_sets[ori_txn_idx].read().unwrap();
        for &key_idx in write_set.iter().chain(read_set.iter()) {
            let tracker_ref = self.trackers.get(&key_idx).unwrap();
            let tracker = tracker_ref.read().unwrap();
            if let Some(txn_idx) = tracker
                .finalized_writes
                .range(..ShardedTxnIndexV2::new(round_id, shard_id, 0))
                .last()
            {
                let src_txn_idx = ShardedTxnIndex {
                    txn_index: *self.final_idxs_by_pre_partitioned[txn_idx.pre_partitioned_txn_idx]
                        .read()
                        .unwrap(),
                    shard_id: txn_idx.shard_id(),
                    round_id: txn_idx.round_id(),
                };
                deps.add_required_edge(src_txn_idx, tracker.storage_location.clone());
            }
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

**File:** aptos-move/block-executor/src/executor.rs (L1174-1176)
```rust
        if let Some(hook) = &self.transaction_commit_hook {
            hook.on_transaction_committed(txn_idx, &committed_output);
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

**File:** aptos-move/aptos-vm/src/sharded_block_executor/messages.rs (L13-18)
```rust
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct RemoteTxnWrite {
    state_key: StateKey,
    // The write op is None if the transaction is aborted.
    write_op: Option<WriteOp>,
}
```
