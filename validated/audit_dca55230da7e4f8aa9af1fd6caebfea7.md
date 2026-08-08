### Title
Attacker can keep the banking-stage priority floor artificially elevated to have sigverify censor other users' transactions - ([File: core/src/banking_stage/transaction_scheduler/scheduler_controller.rs])

### Summary
The PoolTogether bug lets a single unprivileged actor perform a cheap, minimal action (claiming one prize at a loss) to keep a shared protocol threshold ("largest tier claimed") artificially pinned in a state that benefits the attacker and denies service to other users. Agave has a structurally similar shared-threshold mechanism: the banking-stage scheduler publishes a `SchedulerPriorityFloor` once its transaction buffer becomes "saturated", and `sigverify` uses that floor to drop any incoming transaction at or below it, before it ever reaches the mempool/scheduler.

### Finding Description
`SaturationState` in `scheduler_controller.rs` tracks whether the scheduler's retained transaction buffer is "saturated" and, while saturated, publishes the current minimum priority in the container as a shared `SchedulerPriorityFloor` [1](#0-0) . The floor is a cross-stage, mutable shared state (`AtomicU64`) consumed by the ingress/sigverify path to drop "at-or-below-floor" transactions before they even reach the scheduler [2](#0-1) .

The state transition into "saturated" is sticky and cheap to trigger: it becomes `true` as soon as `buffer_size >= saturation_watermark` (99% of capacity) **or** even a single `num_dropped_on_capacity > 0` event occurs, and it only reverts to `false` once the buffer drops below the desaturation watermark (95%) **and** there have been zero capacity drops in that check [3](#0-2) . Once saturated, the published floor tracks the container's current minimum priority, which is derived from `TransactionStateContainer::get_min_max_priority` [4](#0-3) , and the container itself evicts the lowest-priority transaction whenever it is pushed past capacity [5](#0-4) .

This mirrors the PT bug's mechanics precisely: a small, cheap, repeatable action by a single unprivileged sender (submitting just enough low-fee transactions to keep the buffer near/at capacity, or to occasionally trip a capacity-drop event) is sufficient to keep the shared "saturated" state pinned active, and thereby keep the published floor elevated to whatever the current minimum-priority transaction is. This is analogous to the PT attacker claiming a single low-value prize at a loss purely to keep a shared tier-state variable "active" and thereby suppress the mechanism (bots claiming/tier-shrinking) that would otherwise help other users.

### Impact Explanation
While the floor is "saturated," `sigverify` drops any newly arriving transaction whose priority is at or below the published floor, regardless of whether that transaction is otherwise valid and fee-paying [6](#0-5) . A malicious, low-cost sender can keep the buffer near the 99%/95% band (well within reach for a single high-throughput but cheap connection) to keep the scheduler in the "saturated" state semi-permanently on a target leader, causing that leader node to systematically pre-filter out other unprivileged users' transactions at the sigverify boundary — a local, leader-scoped censorship/DoS effect against ordinary users, without needing stake or validator privileges. This does not corrupt consensus (each leader computes its own floor independently), but it degrades transaction inclusion fairness for legitimate lower/ordinary-fee users on affected leader slots, which is the same class of harm described in the PT finding (users without a "bot"/high-priority action lose access to a service others are unfairly gate-keeping).

### Likelihood Explanation
This requires only sending a sustained stream of packets from a normal (potentially unstaked) client — no validator or operator role is needed — reachable purely via the QUIC/UDP ingestion path feeding `banking_stage`/`scheduler_controller`. The saturation trigger is intentionally sensitive (a single capacity-drop event, or 99% buffer fill) and the desaturation condition is comparatively strict (must fall to 95% AND zero drops), making the "stuck saturated" state easy to sustain with modest, cheap traffic. This is a purely local/per-leader effect and is bounded by that leader's slot window, which keeps it plausible without needing multiple clients or excessive request rates.

### Recommendation
Make the saturation/desaturation and floor-publication logic resistant to being pinned by a single low-cost sender: e.g., base the floor on a smoothed/aggregated statistic (e.g., an EMA of drop rate or of true buffer occupancy across multiple senders) rather than an instantaneous, single-event trigger; require sustained saturation across multiple observation windows before publishing/maintaining a non-zero floor; and/or exclude self-inflicted capacity pressure from a single sender/IP/fee-payer from contributing to the saturation determination (analogous to PT's mitigation of making `largestTierClaimed` resistant to being set by a single claim).

### Proof of Concept
1. A client establishes a QUIC connection to a target leader's TPU ingest and continuously submits many small, minimally-fee-paying transactions (any valid signed transactions with the lowest acceptable priority) at a rate sufficient to keep `TransactionStateContainer::buffer_size()` at or above `saturation_watermark` (99% of capacity), or to occasionally exceed capacity and trigger at least one `num_dropped_on_capacity > 0` event, per `SaturationState::update` [3](#0-2) .
2. Once `saturated == true`, `update_scheduler_priority_floor` publishes `priority_floor = container.get_min_max_priority().min` to the shared `SchedulerPriorityFloor` used by sigverify [7](#0-6) .
3. Any concurrent transaction submitted by another (unprivileged, non-attacker) user with priority at or below this floor is dropped at sigverify ingestion before ever reaching the scheduler [6](#0-5) .
4. By continually feeding the buffer to keep it above the 95% desaturation watermark (or periodically re-triggering a capacity drop), the attacker keeps the floor elevated indefinitely on that leader's slots, denying inclusion to other users' transactions whose priority does not exceed the attacker-influenced floor — cheaply, and without needing validator/stake privileges.

Note: I was unable to fully trace every call site inside `core/src/sigverify.rs` (large file, only the module doc line was retrievable via the available tooling) to confirm the exact drop condition wording; the semantics are documented at the `SchedulerPriorityFloor` definition itself ("Sigverify drops at-or-below-floor arrivals") [6](#0-5) . If deeper verification of the exact sigverify integration is needed, a Devin session with full file access to `core/src/sigverify.rs` and `core/src/sigverify_stage.rs` would be required.

### Citations

**File:** core/src/banking_stage/transaction_scheduler/scheduler_controller.rs (L67-106)
```rust
/// Detects saturation and publishes the priority floor for dropping low-priority transactions upstream.
struct SaturationState {
    priority_floor: Arc<SchedulerPriorityFloor>,
    saturated: bool,
    saturation_watermark: usize,
    desaturation_watermark: usize,
}

impl SaturationState {
    fn new(priority_floor: Arc<SchedulerPriorityFloor>, container_capacity: usize) -> Self {
        let saturation_watermark =
            container_capacity.saturating_mul(SATURATION_BUFFER_PCT as usize) / 100;
        let desaturation_watermark =
            container_capacity.saturating_mul(DESATURATION_BUFFER_PCT as usize) / 100;
        Self {
            priority_floor,
            saturated: false,
            saturation_watermark,
            desaturation_watermark,
        }
    }

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

**File:** core/src/banking_stage/transaction_scheduler/transaction_state_container.rs (L220-225)
```rust
    fn get_min_max_priority(&self) -> Option<(u64, u64)> {
        let min = self.priority_queue.first()?.priority;
        let max = self.priority_queue.last().unwrap().priority;

        Some((min, max))
    }
```
