Now I have all the information needed for a complete analysis. Let me trace the full execution path.

**Key findings from the code:**

1. `TransactionCommitHook` trait (`txn_commit_hook.rs:8-9`) has **only one method**: `on_transaction_committed`. There is no `on_execution_aborted` or abort handler.

2. `CrossShardCommitSender::send_remote_update_for_success` (`cross_shard_client.rs:102-125`) iterates **only over the write set** of the committed output. Keys declared in `dependent_edges` but absent from the write set receive **no message**.

3. `RemoteStateValue::get_value` (`remote_state_value.rs:29-38`) calls `cvar.wait(status)` with **no timeout** — it blocks indefinitely until `set_value` is called.

4. `CrossShardCommitReceiver::start` (`cross_shard_client.rs:30-44`) only stops when it receives `CrossShardMsg::StopMsg`, which is sent by the **execution thread after it finishes** (`sharded_executor_service.rs:163-167`). If the execution thread is blocked on `get_value`, it never sends `StopMsg` — both threads deadlock.

**The deadlock chain:**
- Source shard: transaction T_A aborts (Move abort / gas exhaustion) → write set contains only gas fee deduction, not key K → `send_remote_update_for_success` iterates write set, never finds K → no `RemoteTxnWriteMsg` sent for K.
- Dependent shard: execution thread calls `get_state_value(K)` → `RemoteStateValue::get_value` → `cvar.wait(status)` → blocks forever. `StopMsg` is never sent → `CrossShardCommitReceiver` also blocks on `receive_cross_shard_msg` forever.

**Attacker model:** An unprivileged user submits a transaction that the partitioner's static analysis (`AnalyzedTransaction`) predicts will write to key K, but which aborts at runtime. This is trivially achievable (e.g., a transaction that runs out of gas after the partitioner's pre-execution analysis, or a Move `abort` that fires conditionally). No validator control or privileged access is required.

**Guard check:** There is no compensating mechanism. The `TransactionCommitHook` trait has no abort callback. `CrossShardStateView::set_value` is only reachable via `CrossShardCommitReceiver`, which only processes `RemoteTxnWriteMsg` messages. The `Waiting` state has no timeout path.

---

### Title
Unbounded `cvar.wait` in `RemoteStateValue::get_value` causes permanent shard executor deadlock when a cross-shard source transaction aborts without writing the declared dependency key — (`aptos-move/aptos-vm/src/sharded_block_executor/remote_state_value.rs`)

### Summary
`CrossShardCommitSender::send_remote_update_for_success` only notifies dependent shards for keys present in the committed transaction's write set. When a source transaction aborts (Move abort, gas exhaustion) and does not write a key declared as a cross-shard dependency, no `RemoteTxnWriteMsg` is ever sent for that key. The dependent shard's execution thread blocks indefinitely on `RemoteStateValue::get_value` → `cvar.wait(status)`, which has no timeout. Because the execution thread never completes, it never sends `CrossShardMsg::StopMsg` to itself, so the `CrossShardCommitReceiver` thread also blocks permanently. Both threads in the dependent shard are wedged for the lifetime of the block execution.

### Finding Description

`RemoteStateValue::get_value` performs an unconditional condvar wait:

```rust
while let RemoteValueStatus::Waiting = *status {
    status = cvar.wait(status).unwrap();   // no timeout
}
``` [1](#0-0) 

`set_value` is only called from `CrossShardCommitReceiver::start` upon receiving a `RemoteTxnWriteMsg`:

```rust
RemoteTxnWriteMsg(txn_commit_msg) => {
    let (state_key, write_op) = txn_commit_msg.take();
    cross_shard_state_view.set_value(&state_key, write_op.and_then(|w| w.as_state_value()));
},
``` [2](#0-1) 

`RemoteTxnWriteMsg` is only sent from `send_remote_update_for_success`, which iterates the **write set** of the committed output:

```rust
for (state_key, write_op) in txn_output.write_set().expect_write_op_iter() {
    if let Some(dependent_shard_ids) = edges.get(state_key) {
        // send message
    }
}
``` [3](#0-2) 

Keys present in `dependent_edges` but **absent from the write set** (because the transaction aborted before writing them) are silently skipped — no message is sent, no `None` sentinel is dispatched.

The `TransactionCommitHook` trait has no abort callback:

```rust
pub trait TransactionCommitHook<O>: Send + Sync {
    fn on_transaction_committed(&self, txn_idx: TxnIndex, output: &O);
}
``` [4](#0-3) 

`CrossShardCommitSender` implements only `on_transaction_committed` — there is no path to notify dependent shards when a transaction aborts without writing a declared key. [5](#0-4) 

The `StopMsg` that terminates `CrossShardCommitReceiver` is sent only after the execution thread finishes:

```rust
cross_shard_client_clone.send_cross_shard_msg(shard_id, round, CrossShardMsg::StopMsg);
``` [6](#0-5) 

If the execution thread is blocked on `get_value`, `StopMsg` is never sent, and `CrossShardCommitReceiver` also blocks on `receive_cross_shard_msg` forever. [7](#0-6) 

### Impact Explanation
Both the execution thread and the `CrossShardCommitReceiver` thread of the dependent shard are permanently wedged. The rayon thread pool for that shard is exhausted. The coordinator (`LocalExecutorClient::get_output_from_shards`) blocks indefinitely on `result_rx.recv()`, stalling the entire block execution pipeline. Under the sharded block executor, this constitutes a **permanent chain availability failure** for any block containing such a transaction pair. [8](#0-7) 

### Likelihood Explanation
Any transaction that the partitioner's static analysis (`AnalyzedTransaction`) predicts will write to a key, but which aborts at runtime (Move `abort`, gas exhaustion, arithmetic overflow, etc.), triggers this. This is a normal operational condition, not an exotic edge case. An attacker can deliberately craft a transaction that passes static write-set analysis but aborts at runtime (e.g., a function that conditionally aborts based on on-chain state the attacker controls). No privileged access is required.

### Recommendation
In `CrossShardCommitSender::on_transaction_committed` (or in a new `on_execution_aborted` hook added to `TransactionCommitHook`), after processing the write set, iterate over all keys in `dependent_edges[txn_idx]` that were **not** covered by the write set and send a `RemoteTxnWriteMsg` with `write_op = None` for each. This ensures `CrossShardStateView::set_value` is always called for every declared dependency key, unblocking `get_value` with a `Ready(None)` result regardless of whether the source transaction succeeded or aborted.

### Proof of Concept

```rust
#[test]
fn test_get_value_blocks_forever_without_set_value() {
    use std::{sync::Arc, thread, time::Duration};
    use aptos_vm::sharded_block_executor::remote_state_value::RemoteStateValue;

    let rsv = Arc::new(RemoteStateValue::waiting());
    let rsv_clone = rsv.clone();

    let handle = thread::spawn(move || {
        rsv_clone.get_value() // blocks indefinitely
    });

    // Give the thread time to enter the wait
    thread::sleep(Duration::from_millis(200));

    // Assert the thread has NOT terminated (set_value was never called)
    assert!(
        !handle.is_finished(),
        "get_value returned without set_value being called — invariant violated"
    );

    // Cleanup: unblock to avoid test hang
    rsv.set_value(None);
    handle.join().unwrap();
}
```

This directly demonstrates that `get_value` blocks indefinitely when `set_value` is never called — exactly the condition produced when a source transaction aborts without writing a declared cross-shard dependency key.

### Citations

**File:** aptos-move/aptos-vm/src/sharded_block_executor/remote_state_value.rs (L32-33)
```rust
        while let RemoteValueStatus::Waiting = *status {
            status = cvar.wait(status).unwrap();
```

**File:** aptos-move/aptos-vm/src/sharded_block_executor/cross_shard_client.rs (L30-44)
```rust
        loop {
            let msg = cross_shard_client.receive_cross_shard_msg(round);
            match msg {
                RemoteTxnWriteMsg(txn_commit_msg) => {
                    let (state_key, write_op) = txn_commit_msg.take();
                    cross_shard_state_view
                        .set_value(&state_key, write_op.and_then(|w| w.as_state_value()));
                },
                CrossShardMsg::StopMsg => {
                    trace!("Cross shard commit receiver stopped for round {}", round);
                    break;
                },
            }
        }
    }
```

**File:** aptos-move/aptos-vm/src/sharded_block_executor/cross_shard_client.rs (L105-124)
```rust
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

**File:** aptos-move/block-executor/src/txn_commit_hook.rs (L8-10)
```rust
pub trait TransactionCommitHook<O>: Send + Sync {
    fn on_transaction_committed(&self, txn_idx: TxnIndex, output: &O);
}
```

**File:** aptos-move/aptos-vm/src/sharded_block_executor/sharded_executor_service.rs (L163-167)
```rust
                    cross_shard_client_clone.send_cross_shard_msg(
                        shard_id,
                        round,
                        CrossShardMsg::StopMsg,
                    );
```

**File:** aptos-move/aptos-vm/src/sharded_block_executor/local_executor_shard.rs (L164-175)
```rust
    fn get_output_from_shards(&self) -> Result<Vec<Vec<Vec<TransactionOutput>>>, VMStatus> {
        let _timer = WAIT_FOR_SHARDED_OUTPUT_SECONDS.start_timer();
        trace!("LocalExecutorClient Waiting for results");
        let mut results = vec![];
        for (i, rx) in self.result_rxs.iter().enumerate() {
            results.push(
                rx.recv()
                    .unwrap_or_else(|_| panic!("Did not receive output from shard {}", i))?,
            );
        }
        Ok(results)
    }
```
