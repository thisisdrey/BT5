### Title
Priority-blind FIFO eviction on the sigverify→banking-stage channel lets low-fee packet floods evict already-verified high-priority transactions before scheduling - (File: `core/src/banking_trace.rs`)

### Summary
The channel connecting `SigVerifyWorkerPool::run_transaction_task` to the banking stage (`BankingPacketSender`/`TracedSender` wrapping `EvictingSender`) is bounded and evicts the **oldest** queued batch whenever a new batch arrives and the channel is full, with no regard to transaction fee/priority. An unprivileged attacker who sustains a flood of packets that individually pass dedup and sigverify (i.e., valid signatures, arbitrarily low fee) can fill this channel faster than the scheduler drains it, causing already-verified, higher-fee transactions still sitting in the channel to be evicted purely due to FIFO age, before the scheduler's own priority-aware logic ever sees them.

### Finding Description
Each sigverify worker, after `dedup`+`ed25519_verify_serial` succeed, pushes the verified batch into `state.banking_stage_sender.send(banking_packet_batch.clone())`: [1](#0-0) 

This sender is a `TracedSender` wrapping `EvictingSender<BankingPacketBatch>`: [2](#0-1) 

The underlying `EvictingSender::try_send` implementation evicts the single oldest entry in the channel whenever it's full, unconditionally on age, never on priority/fee: [3](#0-2) 

The channel capacity is fixed at `NON_VOTE_CHANNEL_CAPACITY = 1024 * 16`: [4](#0-3) 

The only priority-aware defense in this pipeline, `SchedulerPriorityFloor`, is driven exclusively by saturation of the *scheduler's internal* `TransactionStateContainer` buffer (`TOTAL_BUFFERED_PACKETS`), not by occupancy of the `banking_stage_sender` channel itself: [5](#0-4) 

So the floor is only published once the downstream container is nearly full — it does nothing to protect batches that are still waiting in the sigverify→banking-stage channel, which is drained by `TransactionViewReceiveAndBuffer::receive_and_buffer_packets` in bounded bursts (`PACKET_BURST_LIMIT = 1000` per `PACKET_BURST_TIMEOUT = 1ms`), interleaved with scheduling/consuming work each loop iteration: [6](#0-5) 

Because sigverify runs on multiple parallel worker threads and cheap, low-fee transactions are just as fast to sign/verify as high-fee ones, an attacker can push verified low-fee batches into the channel at a rate exceeding the scheduler's drain rate. Once the channel of 16,384 slots is full, every additional accepted batch evicts the oldest resident batch — which may be a legitimate high-fee transaction that arrived earlier and is still waiting to be drained, has nothing to do with its priority.

### Impact Explanation
This is a liveness/fairness violation (QoS evasion) rather than memory-unboundedness: the channel stays bounded by design, but the fairness invariant "every verified packet is fairly considered by fee/priority" is broken at this specific hop. A legitimate high-fee/high-priority transaction, once verified and enqueued, can be silently dropped and never reach the scheduler at all — it is evicted before `update_scheduler_priority_floor`/the container's own fee-based drop logic (`push_ids_into_queue`, which explicitly drops lowest-priority entries when full) ever gets a chance to apply. This matches the "legitimate high-value transactions being evicted before scheduling" impact described in the question and falls in the QoS-evasion/liveness-DoS bounty category, since it degrades fair inclusion of paying users' transactions during any sustained low-fee flood.

### Likelihood Explanation
Preconditions: an unprivileged client only needs to send a sustained stream of distinct, validly-signed, low-fee transactions to the public TPU (UDP/QUIC) fast enough to keep the non-vote channel (16,384 slots) saturated for the duration a target high-fee transaction would otherwise wait to be drained. This requires no special access, staking, or leader control — just raw throughput, and is fully consistent with the attacker model in scope. Feasibility depends on out-verifying the scheduler's drain rate, which is plausible since dedup+ed25519 verification cost is independent of fee and runs on multiple parallel worker threads, while draining is capped per loop iteration (`PACKET_BURST_LIMIT`) and shares CPU time with other scheduler work. The condition is repeatable for the duration of the flood.

### Recommendation
Make eviction in the sigverify→banking-stage channel priority-aware instead of pure FIFO-by-age: e.g., replace/extend `EvictingSender` with a bounded structure that evicts the lowest-priority entry (using per-packet fee/priority computed via `calculate_priority_from_bytes`, already used by `apply_priority_floor_to_batch`) rather than the oldest, or track channel occupancy in `SaturationState`/`SchedulerPriorityFloor` so the floor also accounts for this upstream channel's fill level, not only the downstream container's.

### Proof of Concept
Integration-test plan (crossbeam-channel based, mirroring `EvictingSender`'s existing usage in `core/src/banking_trace.rs`):

```rust
// pseudo-outline, place near streamer/src/evicting_sender.rs tests or
// core/src/sigverify.rs tests
#[test]
fn test_evicting_sender_drops_high_priority_batch_under_low_priority_flood() {
    use crossbeam_channel::bounded;
    use solana_streamer::{evicting_sender::EvictingSender, streamer::ChannelSend};

    const CAPACITY: usize = 16; // scaled-down NON_VOTE_CHANNEL_CAPACITY
    let (sender, receiver) = bounded(CAPACITY);
    let evicting = EvictingSender::new(sender, receiver.clone());

    // Simulate a verified high-priority batch entering first.
    let high_priority_batch = make_batch(/* high fee tx */);
    evicting.try_send(high_priority_batch.clone()).unwrap();

    // Flood with CAPACITY distinct low-priority, individually-valid batches,
    // as sigverify workers would after dedup+sigverify success.
    for _ in 0..CAPACITY {
        let low_priority_batch = make_batch(/* low/zero fee tx, distinct sig */);
        // ignore result; each send may evict the oldest resident batch
        let _ = evicting.try_send(low_priority_batch);
    }

    // Assert: the high-priority batch sent first is no longer present in the
    // channel/receiver -- it was evicted purely due to age, not priority.
    let remaining: Vec<_> = receiver.try_iter().collect();
    assert!(
        !remaining.iter().any(|b| batch_matches(b, &high_priority_batch)),
        "high-priority batch survived flood of low-priority batches (expected eviction due to FIFO policy)"
    );
}
```

Full end-to-end variant: wire a real `SigVerifyStage` (as in `core/benches/sigverify_stage.rs`) with a small `NON_VOTE_CHANNEL_CAPACITY`, send one high-fee transaction, then flood `CAPACITY` distinct valid low-fee transactions concurrently from a separate thread, and assert via the `verified_r`/banking-stage receiver that the high-fee transaction is never received by the scheduler (`receive_and_buffer_packets`), confirming it was evicted before being fairly considered.

### Citations

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

**File:** core/src/banking_trace.rs (L25-31)
```rust
/// Capacity of the vote channel between sigverify and the banking-stage.
/// Sized to fit all votes from a reasonably sized cluster for 1 slot, + margin.
const VOTE_CHANNEL_CAPACITY: usize = 1024 * 8;

/// Capacity of the non-vote (transaction) channel between sigverify and the banking-stage.
/// Larger than the vote channel to absorb bursty TPU load.
const NON_VOTE_CHANNEL_CAPACITY: usize = 1024 * 16;
```

**File:** core/src/banking_trace.rs (L410-428)
```rust
    /// Send a batch on the channel. This may evict an existing batch to make
    /// room; in that case `Ok(n)` is returned where `n` is the number of
    /// evicted packets. On channel disconnect returns `Err(SendError)`.
    pub fn send(&self, batch: BankingPacketBatch) -> Result<usize, SendError<BankingPacketBatch>> {
        if let Some(ActiveTracer { trace_sender, exit }) = &self.active_tracer
            && !exit.load(Ordering::Relaxed)
        {
            // Ignore errors in sending to tracer - it is a non-critical component.
            let _ = trace_sender.try_send(TimedTracedEvent(
                SystemTime::now(),
                TracedEvent::PacketBatch(self.label, BankingPacketBatch::clone(&batch)),
            ));
        }
        match self.sender.try_send(batch) {
            Ok(()) => Ok(0),
            Err(TrySendError::Full(b)) => Ok(b.len()),
            Err(TrySendError::Disconnected(b)) => Err(SendError(b)),
        }
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

**File:** core/src/banking_stage/transaction_scheduler/scheduler_controller.rs (L333-352)
```rust
    /// Update the scheduler priority floor.
    ///
    /// Semantics: when the retained scheduler buffer is nearly full, drop
    /// arrivals that are at-or-below the current queue-min priority, i.e. no
    /// better than what the bounded scheduler candidate set would evict.
    fn update_scheduler_priority_floor(&mut self, num_dropped_on_capacity: usize) {
        let buffer_size = self.container.buffer_size();
        let saturated = self
            .saturation_state
            .update(buffer_size, num_dropped_on_capacity);
        let priority_floor = if saturated {
            self.container
                .get_min_max_priority()
                .map_or(0, |(min, _)| min)
        } else {
            0
        };

        self.saturation_state.publish_floor(priority_floor);
    }
```

**File:** core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs (L159-228)
```rust
        const RECV_TIMEOUT: Duration = Duration::from_millis(10);
        const PACKET_BURST_TIMEOUT: Duration = Duration::from_millis(1);
        const PACKET_BURST_LIMIT: usize = 1000;
        let start = Instant::now();

        let mut received_message = false;
        let mut stats = ReceivingStats::default();

        // If not leader/unknown, do a blocking-receive initially. This lets
        // the thread sleep until a message is received, or until the timeout.
        // Additionally, only sleep if the container is empty.
        let mut timed_out = false;
        if container.is_empty()
            && matches!(
                decision,
                BufferedPacketsDecision::Forward | BufferedPacketsDecision::ForwardAndHold
            )
        {
            // TODO: Is it better to manually sleep instead, avoiding the locking
            //       overhead for wakers? But then risk not waking up when message
            //       received - as long as sleep is somewhat short, this should be
            //       fine.
            match self.receiver.recv_timeout(RECV_TIMEOUT) {
                Ok(packet_batch_message) => {
                    received_message = true;
                    stats.accumulate(self.handle_packet_batch_message(
                        container,
                        decision,
                        &root_bank,
                        &working_bank,
                        packet_batch_message,
                    ));
                }
                Err(RecvTimeoutError::Timeout) => timed_out = true,
                Err(RecvTimeoutError::Disconnected) => {
                    if !received_message {
                        return Err(DisconnectedError);
                    }
                }
            }
        }

        if !timed_out {
            while start.elapsed() < PACKET_BURST_TIMEOUT && stats.num_received < PACKET_BURST_LIMIT
            {
                let receive_start = Instant::now();
                match self.receiver.try_recv() {
                    Ok(packet_batch_message) => {
                        stats.receive_time_us += receive_start.elapsed().as_micros() as u64;
                        received_message = true;
                        let batch_stats = self.handle_packet_batch_message(
                            container,
                            decision,
                            &root_bank,
                            &working_bank,
                            packet_batch_message,
                        );
                        stats.accumulate(batch_stats);
                    }
                    Err(TryRecvError::Empty) => {
                        break;
                    }
                    Err(TryRecvError::Disconnected) => {
                        if !received_message {
                            return Err(DisconnectedError);
                        }
                    }
                }
            }
        }
```
