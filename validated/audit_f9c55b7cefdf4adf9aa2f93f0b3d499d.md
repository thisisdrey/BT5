### Title
Priority-floor ingress gate uses inclusive `<=` comparison against a saturation-time snapshot of the queue minimum, dropping victim transactions that could still be serviced without eviction - (File: core/src/banking_stage/transaction_scheduler/scheduler_controller.rs, core/src/sigverify.rs)

### Summary
`update_scheduler_priority_floor` publishes `priority_floor = container.get_min_max_priority().min` once the buffer is "saturated" at only `SATURATION_BUFFER_PCT = 99%` of capacity, not at true 100% fullness. `apply_priority_floor_to_batch` in `core/src/sigverify.rs` then drops any arriving packet whose priority is `<= floor` (inclusive) at ingress, before it ever reaches the scheduler container. Because the watermark leaves headroom and the comparison is inclusive rather than strict, transactions that would not actually need to evict anything can be silently discarded ahead of sigverify.

### Finding Description
The relevant logic: [1](#0-0) 

`SaturationState::update` flips `saturated = true` once `buffer_size >= saturation_watermark` (99% of `TOTAL_BUFFERED_PACKETS`) or any drop-on-capacity occurred: [2](#0-1) 

Once saturated, the floor equals the *current* minimum priority in the container - not the priority of a transaction that is actually about to be evicted, and not conditioned on the buffer being literally full. The published floor is read on a separate sigverify thread and applied with an inclusive comparison: [3](#0-2) 

The doc comment for `update_scheduler_priority_floor` explicitly states the intended invariant: reject only arrivals that are "no better than what the bounded scheduler candidate set would evict" [4](#0-3) . This invariant does not hold in two ways:

1. **Margin gap**: saturation triggers at 99% of capacity, so up to ~1% of buffer capacity (plus whatever workers drain between ticks, since `buffer_size` includes in-flight scheduled work per the comment at lines 41-44) may still be free. A transaction whose priority equals the current queue minimum could still fit without evicting anything, yet it is dropped at ingress solely because `priority <= floor`.
2. **Inclusive comparison / cost-free floor manipulation**: an unstaked attacker can flood the leader's TPU with cheap-to-construct transactions (never intended to land, so no fee is ever charged since dropped/evicted transactions are never executed) carrying incrementally higher `priority` (compute-unit price) than a victim's pending resubmission. This pushes `container`'s minimum priority — and hence the published floor — above the victim's resubmitted priority. On the victim's retry, `apply_priority_floor_to_batch` discards the packet at sigverify time, before signature verification, before the transaction ever reaches `receive_and_buffer` or the container, and with no error surfaced to the client (the packet is simply marked `discard`).

The floor is a single global `AtomicU64` (`SchedulerPriorityFloor`) shared across all sigverify workers [5](#0-4) , so this affects all future arrivals cluster-wide until the next scheduler loop tick republishes a floor, and it is recomputed and republished on every iteration of `SchedulerController::run` regardless of whether the specific min-priority transaction it was based on is actually still slated for eviction [6](#0-5) .

No existing guard neutralizes this: sigverify's floor check runs unconditionally under saturation, ahead of sig verification and any per-connection QUIC throttling context; the container-capacity eviction logic is bypassed entirely for these packets since they never reach the container.

### Impact Explanation
This falls under the exact bounty category cited in the question header: "buffer eviction" causing silent drop of another user's fee-paying transaction. A legitimate, fee-paying transaction that could have been serviced (or at minimum queued without forcing any eviction) is discarded at ingress with no feedback to the sender, who will simply see their transaction vanish. Because the floor is published cluster-wide per leader and the attacker's spam transactions cost the attacker nothing (they are evicted from the buffer or dropped before ever being included in a block, so transaction fees are never deducted), the attacker can sustain this state cheaply relative to the damage to honest fee-paying users.

### Likelihood Explanation
Preconditions: sustained buffer occupancy at ≥99% of `TOTAL_BUFFERED_PACKETS` (achievable by any unstaked client with enough packet-sending throughput to a leader's public TPU port — no staked/gossip/config access required) and a victim whose resubmitted priority lands between the true "would actually be evicted" threshold and the attacker-inflated floor. This is a pure volume/priority race that any remote unstaked sender can attempt; it requires no cryptographic bypass, just enough packets at chosen priorities, and is repeatable indefinitely as long as the attacker keeps the buffer at/above the 99% watermark.

### Recommendation
- Use a strict `<` comparison at sigverify (drop only priorities strictly below the floor) instead of `<=`, so ties are never dropped pre-emptively.
- Only enter/publish a non-zero floor when the buffer is truly at hard capacity (100%) and an eviction has actually occurred in that tick (`num_dropped_on_capacity > 0`), rather than at an early 99% soft watermark, or compute the floor from the priority of the *specific* transaction that was just evicted rather than the current queue minimum.
- Consider decaying/expiring the floor faster or re-validating it against the container's live minimum immediately before use, to avoid a stale, attacker-inflated floor persisting for packets received in between scheduler ticks.

### Proof of Concept
Add to `core/src/banking_stage/transaction_scheduler/scheduler_controller.rs`'s `saturation_state_tests` module (or a new integration test) an invariant/fuzz test:

```rust
#[test]
fn floor_never_rejects_a_transaction_that_would_not_be_evicted() {
    // Model: container has capacity C, saturation triggers at 99% of C.
    let capacity = TOTAL_BUFFERED_PACKETS;
    let saturation_watermark = capacity.saturating_mul(SATURATION_BUFFER_PCT as usize) / 100;

    // Fill buffer to exactly the saturation watermark (< capacity), i.e. there
    // IS spare room and nothing needs eviction yet.
    let buffer_size = saturation_watermark;
    let num_dropped_on_capacity = 0;

    let (mut state, floor) = make_state();
    let saturated = state.update(buffer_size, num_dropped_on_capacity);
    assert!(saturated); // watermark reached, floor gets published

    let current_min_priority = 1000u64; // lowest priority currently queued
    state.publish_floor(current_min_priority);

    // A transaction arriving with priority == current_min_priority: since
    // buffer_size < capacity, there is still room and this transaction would
    // NOT need to evict anything to be buffered.
    let victim_priority = current_min_priority;

    // Sigverify's inclusive check incorrectly drops it:
    let would_be_dropped = victim_priority <= floor.get();
    assert!(
        !would_be_dropped,
        "victim tx with priority {victim_priority} was dropped at ingress even \
         though buffer had spare capacity ({buffer_size} < {capacity}) and no \
         eviction was necessary — violates the documented invariant"
    );
}
```

Fuzz extension: vary `attacker_fill_priority` (used to raise `current_min_priority` each tick by evicting lower-priority filler) and `victim_retry_priority`, asserting for all generated sequences that `victim_retry_priority > true_eviction_threshold(buffer_state)` implies `apply_priority_floor_to_batch` never marks the victim packet `discard`. Running this against the real `apply_priority_floor_to_batch` (`core/src/sigverify.rs:413-439`) with a `<=` comparison will fail whenever `victim_priority == floor`, demonstrating the bug; changing the comparison to `<` and/or gating floor publication on true 100% capacity with an actual eviction should make the assertion hold.

### Citations

**File:** core/src/banking_stage/transaction_scheduler/scheduler_controller.rs (L89-106)
```rust
    /// Update the saturation state.
    fn update(&mut self, buffer_size: usize, num_dropped_on_capacity: usize) -> bool {
        if self.saturated {
            if buffer_size < self.desaturation_watermark && num_dropped_on_capacity == 0 {
                self.saturated = false;
            }
        } else if buffer_size >= self.saturation_watermark || num_dropped_on_capacity > 0 {
            self.saturated = true;
        }

        self.saturated
    }

    /// Publish the priority floor.
    fn publish_floor(&self, floor: u64) {
        self.priority_floor.set(floor);
    }
}
```

**File:** core/src/banking_stage/transaction_scheduler/scheduler_controller.rs (L254-272)
```rust
            self.receive_completed()?;
            let _scheduled = self.process_transactions(&decision, cost_pacer.as_ref(), &now)?;
            if decision.bank().is_none() {
                let (_, clean_time_us) = measure_us!(self.incremental_recheck());
                self.timing_metrics.update(|timing_metrics| {
                    timing_metrics.clean_time_us += clean_time_us;
                });
            }
            let receiving_stats = self.receive_and_buffer_packets(&decision).map_err(|_| {
                SchedulerError::DisconnectedRecvChannel("receive and buffer disconnected")
            })?;
            // Report metrics only if there is data.
            // Reset intervals when appropriate, regardless of report.
            let should_report = self.count_metrics.interval_has_data();
            let priority_min_max = self.container.get_min_max_priority();
            self.count_metrics.update(|count_metrics| {
                count_metrics.update_priority_stats(priority_min_max);
            });
            self.update_scheduler_priority_floor(receiving_stats.num_dropped_on_capacity);
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

**File:** core/src/sigverify.rs (L413-439)
```rust
fn apply_priority_floor_to_batch(
    batch: &mut PacketBatch,
    floor: u64,
    bank: &Bank,
) -> (usize, bool) {
    let mut dropped: usize = 0;
    let mut any_kept = false;
    for mut packet in batch.iter_mut() {
        if packet.meta().discard() {
            continue;
        }
        let Some(data) = packet.data(..) else {
            // Zero-length or otherwise unreadable: leave to downstream
            // stages to reject.
            any_kept = true;
            continue;
        };
        // Unparseable packets are kept and left for downstream rejection.
        match calculate_priority_from_bytes(bank, data) {
            Some(priority) if priority <= floor => {
                packet.meta_mut().set_discard(true);
                dropped = dropped.saturating_add(1);
            }
            _ => any_kept = true,
        }
    }
    (dropped, !any_kept)
```

**File:** banking-stage-ingress-types/src/lib.rs (L71-95)
```rust
/// Priority floor shared from the banking-stage scheduler to sigverify.
///
/// When saturated, the scheduler publishes the queue-min transaction's
/// priority. Sigverify drops at-or-below-floor arrivals.
/// In practice, transactions always have non-zero priorities.
#[derive(Debug)]
pub struct SchedulerPriorityFloor(AtomicU64);

impl SchedulerPriorityFloor {
    pub fn new() -> Self {
        Self(AtomicU64::new(0))
    }

    pub fn set(&self, floor: u64) {
        self.0.store(floor, Ordering::Relaxed);
    }

    pub fn clear(&self) {
        self.set(0);
    }

    pub fn get(&self) -> u64 {
        self.0.load(Ordering::Relaxed)
    }
}
```
