### Title
FIFO/recency-based eviction in `EvictingSender` lets low-fee sigverify-passing packets displace already-queued higher-fee packets ahead of banking-stage consumption - ([File: core/src/sigverify.rs])

### Summary
`SigVerifyWorkerState::run_transaction_task` forwards every batch that has at least one valid signature to the banking stage via `TracedSender::send`/`EvictingSender::try_send`, with no fee/priority comparison at eviction time. `EvictingSender::try_send` explicitly evicts the *oldest* queued batch to make room for a *newer* one whenever the bounded channel is full, regardless of the relative fee/priority of either batch. [1](#0-0) [2](#0-1) 

### Finding Description
`EvictingSender::try_send` is used as the transport between sigverify workers and the banking stage for the non-vote channel (`TracedSender::send` in `core/src/banking_trace.rs`). Its eviction rule is: on a full channel, pop whatever is currently at the front (`self.receiver.try_recv()` — the oldest still-unconsumed batch) and push the new batch in its place, returning the evicted batch's length as `eviction_drops`. [3](#0-2) [4](#0-3) 

Nowhere in this path is the evicted batch chosen based on fee/priority — only recency ("Prefer newer messages over older messages"). `run_transaction_task` only applies a priority-aware filter (`apply_priority_floor_to_batch`) to the *incoming* packet before verification, and only when `priority_floor` is `Some` and the scheduler has published a non-zero floor (i.e., only once the downstream scheduler is already saturated). This floor check happens before signature verification and before the channel send, so it can prevent some low-priority packets from ever entering the channel, but it does nothing to protect a legitimate higher-fee batch that is already sitting in the channel waiting to be dequeued by the banking stage's consumer thread — that batch can still be evicted by any subsequent packet, high or low fee, once the queue is full. [5](#0-4) 

An unstaked attacker only needs to:
1. Open a QUIC connection to the TPU port (subject to per-IP/unstaked connection and stream-rate limits, but these still allow up to `MAX_UNSTAKED_TPS` per stream and multiple concurrent unstaked connections/streams). [6](#0-5) 
2. Send disposable, self-signed transactions with the lowest possible fee (sigverify only checks the ed25519 signature — not the fee payer's balance or the actual priority fee value beyond the floor check when saturated).
3. Sustain enough throughput to keep the bounded non-vote channel (`NON_VOTE_CHANNEL_CAPACITY`) full so that every new `send()` forces an eviction.

Because eviction always removes the oldest entry, and "oldest" is unrelated to fee/priority, a stream of low-value attacker batches arriving just fast enough to keep the channel saturated will eventually evict a legitimate high-fee batch that arrived earlier and is still waiting for the banking-stage consumer to drain it — even though the attacker's own batches carry no genuine economic value and may never land (fee payer has no funds, transaction fails at execution). This is a real ingress path (`core/src/sigverify.rs` → `banking_trace.rs::TracedSender::send` → `streamer/src/evicting_sender.rs::EvictingSender::try_send`), reachable purely with unprivileged network access to the TPU port, and does not require staking, gossip control, or any privileged operation.

### Impact Explanation
This falls under the "grossly underpriced pre-fee work / QoS evasion" category referenced in the audit scope: the fee market's implicit invariant is that under congestion, higher-fee transactions should be prioritized for inclusion; a recency-only eviction policy breaks that invariant at the sigverify→banking-stage boundary specifically, letting attacker-controlled batch arrival timing (not fee) decide what survives in the buffer. The scoped impact matches the question precisely: legitimate fee-paying transactions can be evicted by attacker-supplied packets that themselves may never land economically, degrading effective leader throughput/fairness below what fees would imply. [7](#0-6) 

### Likelihood Explanation
Preconditions are modest: the attacker needs only an unstaked QUIC connection to the leader's TPU port and the ability to produce arbitrary valid-signature (but underfunded) transactions, both trivially satisfiable by any remote client. The attack is repeatable continuously as long as the attacker can sustain enough throughput (bounded by per-connection/per-IP stream throttling — `MAX_UNSTAKED_TPS = 200` per stream, `DEFAULT_MAX_QUIC_CONNECTIONS_PER_UNSTAKED_PEER = 8`) to keep the target channel saturated during the attacker's window; the effect is probabilistic (which exact legitimate batch gets evicted depends on arrival ordering) but statistically guaranteed to occur under sustained load since the policy is oldest-first, not priority-first. The `priority_floor` mitigation only partially reduces exposure (it prevents very-low-priority packets from being admitted once the scheduler self-reports saturation) but does not protect already-buffered higher-fee batches from being displaced. [8](#0-7) 

### Recommendation
Make `EvictingSender` (or a wrapper used specifically for the banking-stage packet channel) priority-aware: when full, compare the priority/fee of the incoming batch against the priority/fee of the queued batches and evict the lowest-priority entry rather than unconditionally the oldest one, or reject/drop the incoming low-priority batch instead of displacing anything already queued. Alternatively, extend the existing `SchedulerPriorityFloor`/`apply_priority_floor_to_batch` mechanism to also gate the eviction decision inside `try_send`, so that a batch is only allowed to evict another batch if its own priority is greater.

### Proof of Concept
Rust unit test plan (in `streamer/src/evicting_sender.rs` or a new integration test alongside `core/src/sigverify.rs`):
1. Construct an `EvictingSender::new_bounded(2)` (small capacity) carrying `BankingPacketBatch`/mock structs tagged with a `priority: u64` field.
2. Send one "high-priority" batch (`priority = 1_000_000`) and one "low-priority" batch (`priority = 1`) to fill the channel.
3. Repeatedly `try_send` additional "low-priority" batches (`priority = 1`) simulating an attacker flood.
4. Assert that in the current implementation, the *high-priority* batch is eventually returned as the evicted (`TrySendError::Full(older)`) entry once it becomes the oldest queued item — i.e., recency, not priority, determines eviction, demonstrating that a fee-paying transaction gets dropped purely because of arrival order.
5. Expected assertion under a fixed/patched implementation: eviction should never select the batch with the higher `priority` field while a lower-priority batch is available to evict instead; the test should fail against the current code (proving the bug) and pass only after eviction is made priority-aware.

### Citations

**File:** core/src/sigverify.rs (L61-67)
```rust
    stats: SigVerifyWorkerStats,
    /// Scheduler-published priority floor: when saturated, the scheduler publishes
    /// the queue-min transaction's priority and workers drop at-or-below-floor
    /// arrivals here, ahead of signature verification. `None` disables the
    /// check (e.g. for the vote worker, which is governed by a separate
    /// priority policy in banking stage).
    priority_floor: Option<Arc<SchedulerPriorityFloor>>,
```

**File:** core/src/sigverify.rs (L300-324)
```rust
        let working_bank = sharable_banks.working();

        if let Some(floor) = state.priority_floor.as_ref() {
            let floor = floor.get();
            if floor > 0 {
                let ((dropped, all_below), priority_floor_time_us) = measure_us!(
                    apply_priority_floor_to_batch(&mut batch, floor, &working_bank)
                );
                state
                    .stats
                    .total_priority_floor_time_us
                    .fetch_add(priority_floor_time_us as usize, Ordering::Relaxed);
                if dropped > 0 {
                    state
                        .stats
                        .total_dropped_below_priority_floor
                        .fetch_add(dropped, Ordering::Relaxed);
                }
                if all_below {
                    // Entire batch went below-floor: nothing left to verify or
                    // forward.
                    return true;
                }
            }
        }
```

**File:** core/src/sigverify.rs (L346-369)
```rust
        let banking_packet_batch = BankingPacketBatch::new(batch);
        // Sample backlog before the push: measures consumer health without
        // including this batch's own contribution.
        state
            .stats
            .max_pre_send_len
            .fetch_max(state.banking_stage_sender.len(), Ordering::Relaxed);
        match state
            .banking_stage_sender
            .send(banking_packet_batch.clone())
        {
            Ok(0) => {} // avoid poking atomics if nothing was evicted (typical case)
            Ok(evicted) => {
                // record evicted amount into metrics
                state
                    .stats
                    .eviction_drops
                    .fetch_add(evicted, Ordering::Relaxed);
            }
            Err(err) => {
                error!("sigverify send to banking failed: {err:?}");
                return false;
            }
        }
```

**File:** streamer/src/evicting_sender.rs (L6-10)
```rust
/// A sender implementation that evicts the oldest message when the channel is full.
pub struct EvictingSender<T> {
    sender: Sender<T>,
    receiver: Receiver<T>,
}
```

**File:** streamer/src/evicting_sender.rs (L41-66)
```rust
    fn try_send(&self, msg: T) -> std::result::Result<(), TrySendError<T>> {
        let Err(e) = self.sender.try_send(msg) else {
            return Ok(());
        };

        match e {
            // Prefer newer messages over older messages.
            TrySendError::Full(msg) => match self.receiver.try_recv() {
                Ok(older) => {
                    // Attempt to requeue the newer message.
                    // NB: if multiple senders are used, and another sender is faster than us to send() after we've popped `older`,
                    // our try_send() will fail with Full(msg), in which case we drop the new message.
                    self.sender.try_send(msg)?;
                    // Propagate the error _with the older message_.
                    Err(TrySendError::Full(older))
                }
                // Unlikely race condition -- it was just indicated that the channel is full.
                // Attempt to requeue the message.
                Err(TryRecvError::Empty) => self.sender.try_send(msg),
                // Unreachable in practice since we maintain a reference to both the sender and receiver.
                Err(TryRecvError::Disconnected) => unreachable!(),
            },
            // Unreachable in practice since we maintain a reference to both the sender and receiver.
            TrySendError::Disconnected(_) => unreachable!(),
        }
    }
```

**File:** streamer/src/nonblocking/stream_throttle.rs (L16-19)
```rust
/// Max TPS allowed for unstaked connection
const MAX_UNSTAKED_TPS: u64 = 200;
/// Expected fraction of max TPS to be consumed by unstaked connections
const EXPECTED_UNSTAKED_STREAMS_RATIO: f64 = 0.20;
```
