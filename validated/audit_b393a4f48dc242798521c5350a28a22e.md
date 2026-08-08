### Title
Non-priority-aware `forward_stage_sender` channel lets bulk low-value traffic cause `try_send` `Full` drops of legitimate forward batches - ([File: core/src/sigverify.rs])

### Summary
`SigVerifyWorkerPool::try_forward` (`core/src/sigverify.rs:394-404`) enqueues verified batches into `forward_stage_sender`, a plain bounded `crossbeam_channel` (capacity 50,000, created in `core/src/tpu.rs:275`), using `try_send` with no priority awareness; on `TrySendError::Full` the whole batch is silently dropped. Unlike the downstream `ForwardingStage::buffer_packet_batches` (`core/src/forwarding_stage.rs:270-336`), which maintains a fee-priority ordered `PacketContainer` and evicts the lowest-priority packet when at capacity, this upstream channel has no fee/priority ordering at all — it is FIFO and whichever `try_send` call loses the race when the channel is full is dropped regardless of the fee/priority of its contents.

### Finding Description
Multiple `solSigVerifyNN` worker threads (`SigVerifyWorkerPool::worker_iteration`, `core/src/sigverify.rs:214-264`) concurrently call `Self::run_transaction_task` for every incoming non-vote/vote batch, and when `should_forward` is true (`forward_non_votes` config) call `try_forward(forward_stage_sender, banking_packet_batch, is_tpu_vote)` (`core/src/sigverify.rs:370-371`). `try_forward` performs an unconditional `forward_stage_sender.try_send(...)`, dropping the batch on `Full` with only a `warn!` log (`core/src/sigverify.rs:394-404`).

Because multiple worker threads race on the same bounded channel with no ordering/priority metadata attached to the `try_send` call itself, if an unstaked attacker submits enough valid-signature (but low-fee) transactions concurrently to keep the channel saturated, a legitimate higher-value batch that happens to hit `try_send` while full will be dropped just the same as a low-value one — there is no mechanism at this stage to prefer the higher-priority batch. This is in contrast to `ForwardingStage::buffer_packet_batches`, which explicitly compares `min_priority` against incoming `priority` before evicting (`core/src/forwarding_stage.rs:315-331`); that fairness logic only protects the container inside `ForwardingStage`, not the channel feeding it.

The attacker only needs to be an unstaked client submitting valid-signature transactions over the TPU QUIC port; no stake or privileged access is required to reach `run_transaction_task`/`try_forward`. Existing rate-limit/QoS checks upstream (QUIC stake-weighted throttling) and downstream (data-budget token bucket, priority-container eviction) do not protect this specific enqueue point.

### Impact Explanation
Scoped impact: legitimate transaction forwarding batches can be starved/dropped in favor of a concurrent burst of low-value traffic purely due to non-deterministic channel-full races, which is a QoS-evasion-adjacent issue — an unstaked attacker can consume/contend for downstream forwarding channel capacity without any fee/priority discrimination at the point of admission into `forward_stage_sender`.

### Likelihood Explanation
- Preconditions: `forward_non_votes=true` (block-production forwarding enabled) — a validator configuration precondition, granted per the question.
- The channel capacity is large (50,000 slots, `core/src/tpu.rs:275`), and packets must have valid signatures and pass dedup + (if enabled) priority-floor checks before reaching `try_forward`, so filling this channel to capacity requires sustained, high-throughput generation of valid-signature transactions exceeding the forwarding stage's drain rate (`receive_and_buffer`'s tight `try_recv` loop). This makes the attack feasible only under a genuinely large, sustained flood, not a single/low-cost burst — tempering practical severity but not eliminating the underlying design gap.
- Repeatable: the race condition is deterministic in structure (whichever `try_send` occurs while `Full` loses), even though the timing/outcome is probabilistic.

### Recommendation
Make the sigverify→forwarding handoff priority-aware, e.g., by having `try_forward` attach or precompute a priority key and, on `Full`, compare against a tracked minimum in-flight priority (similar to `ForwardingStage`'s eviction logic) before dropping, or replace the single bounded `crossbeam` channel with a priority-ordered admission structure shared across workers so that low-priority batches are preferentially dropped instead of arbitrary FIFO victims.

### Proof of Concept
```rust
// core/src/sigverify.rs (test module)
#[test]
fn test_try_forward_drops_arbitrary_batch_when_full() {
    let (forward_stage_sender, forward_stage_receiver) = crossbeam_channel::bounded(1);

    // Fill the channel with an "attacker" low-value batch.
    let attacker_batch = make_banking_packet_batch(/* low-value packets */);
    SigVerifyWorkerPool::try_forward(&forward_stage_sender, attacker_batch.clone(), false);
    assert_eq!(forward_stage_receiver.len(), 1);

    // A concurrently-arriving legitimate high-value batch hits Full and is dropped.
    let legit_batch = make_banking_packet_batch(/* high-value packets */);
    SigVerifyWorkerPool::try_forward(&forward_stage_sender, legit_batch.clone(), false);

    // Only the attacker's batch is present; the legitimate batch was silently dropped
    // regardless of its higher fee/priority.
    let (received, _) = forward_stage_receiver.try_recv().unwrap();
    assert_eq!(received, attacker_batch);
    assert!(forward_stage_receiver.try_recv().is_err());
}
```
Extend this into a multi-threaded integration test that spawns N attacker threads calling `try_forward` with low-value batches at high rate concurrently with one thread sending a legitimate high-value batch, and measure the drop rate of the legitimate batch versus attacker share to quantify unfairness. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** core/src/sigverify.rs (L214-264)
```rust
    fn worker(exit: Arc<AtomicBool>, channels: WorkerPoolChannels, forward_non_votes: bool) {
        while !exit.load(Ordering::Relaxed) {
            if !Self::worker_iteration(&channels, forward_non_votes) {
                break;
            }
        }
    }

    /// Returns false if some channel connection is disconnected.
    fn worker_iteration(channels: &WorkerPoolChannels, forward_non_votes: bool) -> bool {
        crossbeam_channel::select! {
            recv(&channels.non_vote_receiver) -> maybe_work => {
                match maybe_work {
                    Ok(batch) => Self::run_transaction_task(
                        batch,
                        false,
                        &channels.forward_stage_sender,
                        forward_non_votes,
                        false,
                        &channels.sharable_banks,
                        &channels.non_vote_state,
                    ),
                    Err(_) => false,
                }
            }
            recv(&channels.tpu_vote_receiver) -> maybe_work => {
                match maybe_work {
                    Ok(batch) => Self::run_transaction_task(
                        batch,
                        true,
                        &channels.forward_stage_sender,
                        true,
                        true,
                        &channels.sharable_banks,
                        &channels.tpu_vote_state,
                    ),
                    Err(_) => false,
                }
            }
            recv(&channels.gossip_receiver) -> maybe_work => {
                match maybe_work {
                    Ok(work) => Self::run_gossip_task(
                        work,
                        &channels.gossip_verified_vote_sender,
                    ),
                    Err(_) => false,
                }
            }
            default(Duration::from_millis(10)) => { true }
        }
    }
```

**File:** core/src/sigverify.rs (L394-404)
```rust
    fn try_forward(
        forward_stage_sender: &Sender<(BankingPacketBatch, bool)>,
        banking_packet_batch: BankingPacketBatch,
        is_tpu_vote: bool,
    ) {
        if let Err(TrySendError::Full(_)) =
            forward_stage_sender.try_send((banking_packet_batch, is_tpu_vote))
        {
            warn!("forwarding stage channel is full, dropping packets.");
        }
    }
```

**File:** core/src/forwarding_stage.rs (L315-336)
```rust
            // If at capacity, check lowest priority item.
            if self.packet_container.is_full() {
                let min_priority = self.packet_container.min_priority().expect("not empty");
                // If priority of current packet is not higher than the min
                // drop the current packet.
                if min_priority >= priority {
                    self.metrics.votes_dropped_on_capacity += vote_count;
                    self.metrics.non_votes_dropped_on_capacity += non_vote_count;
                    continue;
                }

                let dropped_packet = self.packet_container.pop_min().expect("not empty");
                self.metrics.votes_dropped_on_capacity +=
                    usize::from(dropped_packet.meta().is_simple_vote_tx());
                self.metrics.non_votes_dropped_on_capacity +=
                    usize::from(!dropped_packet.meta().is_simple_vote_tx());
            }

            self.packet_container
                .insert(packet.to_bytes_packet(), priority);
        }
    }
```

**File:** core/src/tpu.rs (L275-292)
```rust
        let (forward_stage_sender, forward_stage_receiver) = bounded(50_000);

        // Shared between sigverify and scheduler. The scheduler publishes
        // a priority floor under saturation; sigverify reads it and drops
        // below-floor packets ahead of signature verification.
        let scheduler_priority_floor = Arc::new(SchedulerPriorityFloor::new());

        let (sigverify_stage, gossip_sigverify_handle) = SigVerifyStage::new(
            packet_receiver,
            vote_packet_receiver,
            non_vote_sender,
            tpu_vote_sender,
            forward_stage_sender.clone(),
            tpu_sigverify_threads,
            enable_block_production_forwarding,
            bank_forks.read().unwrap().sharable_banks(),
            Some(scheduler_priority_floor.clone()),
        );
```
