### Title
Attacker-controlled priority-floor saturation lets a flooder deterministically drop specific victim fee-paying transactions at sigverify ingress - ([File: core/src/banking_stage/transaction_scheduler/scheduler_controller.rs], [core/src/sigverify.rs])

### Summary
When the scheduler's retained buffer nears capacity, `SaturationState::update` marks the controller saturated and `update_scheduler_priority_floor` publishes `container.get_min_max_priority().0` (the queue's current minimum priority) as a shared `SchedulerPriorityFloor` [1](#0-0) . Sigverify workers then discard, pre-verification, any arriving packet whose computed priority is `<= floor` [2](#0-1) . Because the floor is simply "whatever the current queue-minimum happens to be," and the queue-minimum is influenced by whatever transactions currently occupy the buffer, an attacker who fills the buffer with self-chosen priority values directly controls which priority band gets published as the floor and thus which incoming victim transactions get dropped before they are even considered by the actual bounded-capacity trim logic in `push_ids_into_queue`.

### Finding Description
The relevant flow, all reachable by an unstaked remote sending arbitrary QUIC/UDP TPU traffic:

1. Attacker sends a large volume of distinct, sanitizable, fee-paying transactions with priority `P` (chosen via a modest but non-negligible priority fee). These are accepted through sigverify, parsed, fee-payer-checked, and inserted into the container in `TransactionViewReceiveAndBuffer::handle_packet_batch_message` via `container.push_ids_into_queue` [3](#0-2) .
2. Once `id_to_transaction_state.len()` (buffer_size) reaches/exceeds capacity, `push_ids_into_queue` evicts the lowest-priority entries currently in the BTreeSet queue, and returns `num_dropped` [4](#0-3) .
3. On each scheduler loop iteration, `update_scheduler_priority_floor` computes `buffer_size`/`num_dropped_on_capacity`, feeds them to `SaturationState::update` (saturates at 99% of `TOTAL_BUFFERED_PACKETS`, or immediately on any capacity drop) [5](#0-4) , and when saturated publishes `get_min_max_priority().0` — literally the minimum priority currently sitting in the queue — as the floor [6](#0-5) .
4. Sigverify workers read this floor and drop any subsequently arriving packet with `priority <= floor` before verification/queueing, via `apply_priority_floor_to_batch` [2](#0-1) .

The core design intent, per the code comments, is that the floor should reflect "no better than what the bounded scheduler candidate set would evict" — i.e., a self-consistent backpressure signal so that arrivals doomed to be evicted anyway are dropped earlier to save sigverify work. However, the floor is derived purely from whatever the queue's current minimum happens to be, and that minimum is attacker-steerable: an attacker who saturates the buffer with many transactions all priced at exactly `P` (all above any legitimate low-fee traffic that they want to exclude) pushes the queue-min up to `P`, causing the floor to be published at `P`. Any victim's legitimate transaction priced at or below `P` — even if it would fit in the buffer once older/expiring transactions clear, or even if it's within the top-`capacity` window of "reasonable" pending traffic — is now unconditionally discarded at sigverify, before it's ever considered by the buffer's own bounded eviction logic (`push_ids_into_queue`), which is the actual, principled capacity-bounded mechanism.

This differs qualitatively from ordinary "buffer is full, lowest-fee tx gets evicted" behavior: instead of the victim's tx being fairly weighed against the buffer's actual current lowest-priority occupant on a per-arrival basis (`push_ids_into_queue`'s min-priority eviction, which is symmetric and re-evaluated per push), the sigverify pre-filter applies a floor that is a somewhat stale, coarse-grained snapshot (`min` priority as of the last scheduler-loop tick, held until desaturation) computed from whichever tier of traffic the attacker chose to flood at. Because the attacker fully controls the volume and priority value of the flood, they can drive the published floor to sit exactly at or above the victim's priority tier, giving them the ability to select victim priority bands for silent ingress-level drops — as opposed to the buffer merely enforcing a size bound.

### Impact Explanation
This is a targeted, attacker-steerable denial-of-service against specific fee-tier transactions: an unstaked remote client can, without controlling any validator or peer, cause the leader to silently discard other users' fee-paying transactions at the sigverify stage (`num_dropped_on_capacity`/`total_dropped_below_priority_floor`), even for transactions that could otherwise have been legitimately buffered/considered by the scheduler. This maps to a QoS-evasion / grossly-underpriced-work class issue — the attacker uses cheap flooding to control which fee tier of a competitor is starved, rather than the buffer's capacity bound alone regulating drops.

### Likelihood Explanation
Feasible with only unprivileged QUIC/UDP access to the leader's public TPU port and the ability to sustain enough stream/connection throughput to keep `container.buffer_size()` near `TOTAL_BUFFERED_PACKETS` (99% watermark) or to trigger at least one capacity drop, both attacker-controllable given existing QUIC connection/stream limits are per-connection, not global-priority-aware. The attacker only needs to choose and hold a consistent priority fee `P` slightly above the victim's fee tier and sustain volume; this is fully repeatable and requires no staking or leader control.

### Recommendation
Do not derive the published floor solely from the instantaneous queue-minimum, which an attacker can pin to an arbitrary value by flooding at a chosen priority. Instead, base the floor on a metric resistant to short-window flooding by a single actor, e.g.: require the floor to only rise gradually (rate-limited/EMA-smoothed) rather than snapping to whatever the current min is; or incorporate distinctness/diversity (e.g., per-fee-payer or per-connection quotas already used elsewhere in ingress) before letting flood-inserted transactions influence the published floor; or don't drop at sigverify at all for near-floor priorities and instead rely purely on `push_ids_into_queue`'s existing, symmetric, per-arrival eviction bound.

### Proof of Concept
Extend the existing `saturation_state_tests` module in `core/src/banking_stage/transaction_scheduler/scheduler_controller.rs` with an integration-style test using `TransactionViewReceiveAndBuffer` (as in `receive_and_buffer.rs`'s `test_receive_and_buffer_overfull`):

```rust
#[test]
fn attacker_can_steer_priority_floor_to_drop_victim_tier() {
    // 1. Build a TransactionViewStateContainer with a small TEST_CONTAINER_CAPACITY.
    // 2. Flood with (capacity) transactions all priced at priority P (attacker),
    //    via receive_and_buffer_packets, until buffer_size >= saturation_watermark
    //    or num_dropped_on_capacity > 0.
    // 3. Run update_scheduler_priority_floor with the returned num_dropped_on_capacity;
    //    assert floor.get() == P (the attacker-chosen value), not some
    //    capacity-only-derived quantity independent of attacker input.
    // 4. Submit a victim transaction with priority P (same or lower, distinct fee payer)
    //    through the same sigverify apply_priority_floor_to_batch path;
    //    assert it is marked discard()==true purely because floor == P,
    //    even though nothing about the victim tx's own merits caused the drop.
    // 5. Vary P across repeated runs to show the attacker fully controls
    //    which priority tier is discarded, demonstrating floor is attacker-steerable
    //    rather than solely capacity-bounded.
}
```
Expected assertions: `floor.get()` tracks the attacker-chosen `P` exactly (not a value computed independent of attacker priority selection), and the victim's identical/lower-priority transaction is discarded at `apply_priority_floor_to_batch` prior to any scheduler-level capacity/eviction check being applied to it individually.

### Citations

**File:** core/src/banking_stage/transaction_scheduler/scheduler_controller.rs (L90-100)
```rust
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
```

**File:** core/src/banking_stage/transaction_scheduler/scheduler_controller.rs (L338-352)
```rust
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

**File:** core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs (L342-360)
```rust
            let transaction_id = container.insert_map_only(state);
            let priority_id = TransactionPriorityId::new(priority, transaction_id);

            // Now, if this is a nonce transaction, we know it is validated and higher-priority than any
            // which may exist in the priority queue. If one is queued, evict it. Regardless, record the
            // incoming nonce transaction's nonce as in-use.
            if let Some(nonce_address) = validated_nonce_address {
                if let Some(existing_nonce_priority_id) =
                    container.get_nonce_transaction_priority_id(&nonce_address)
                {
                    receiving_stats.num_evicted_on_nonce_dedup += 1;
                    container.remove_by_id(existing_nonce_priority_id.id);
                }
                container.set_nonce_transaction_priority_id(&nonce_address, priority_id);
            }

            // Transaction is already fully validated and can be inserted into priority queue.
            receiving_stats.num_dropped_on_capacity +=
                container.push_ids_into_queue(std::iter::once(priority_id));
```

**File:** core/src/banking_stage/transaction_scheduler/transaction_state_container.rs (L178-201)
```rust
    fn push_ids_into_queue(
        &mut self,
        priority_ids: impl Iterator<Item = TransactionPriorityId>,
    ) -> usize {
        for id in priority_ids {
            self.priority_queue.insert(id);
        }

        // The number of items in the `id_to_transaction_state` map is
        // greater than or equal to the number of elements in the queue.
        // To avoid the map going over capacity, we use the length of the
        // map here instead of the queue.
        let num_dropped = self
            .id_to_transaction_state
            .len()
            .saturating_sub(self.capacity);

        for _ in 0..num_dropped {
            let priority_id = self.priority_queue.pop_first().expect("queue is not empty");
            self.remove_state(priority_id.id);
        }

        num_dropped
    }
```
