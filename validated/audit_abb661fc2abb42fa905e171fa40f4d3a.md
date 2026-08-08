### Title
Fee-blind FIFO eviction in the sigverify→banking-stage `EvictingSender` lets unfunded, high-declared-priority spam evict already-verified paying transactions - ([File: core/src/sigverify.rs])

### Summary
The channel between `SigVerifyWorkerPool::run_transaction_task` and banking stage (`SigVerifyWorkerState::banking_stage_sender`, backed by `EvictingSender`) discards its *oldest enqueued batch* whenever it is full, with no notion of transaction priority or fee-payer solvency. Because the upstream `SchedulerPriorityFloor` check in `apply_priority_floor_to_batch`/`calculate_priority_from_bytes` derives "priority" purely from the transaction's self-declared `compute_unit_price` (not actual fee-payer balance), an unprivileged attacker can flood the TPU with signed-but-unfunded transactions carrying a maximal declared priority, pass both the priority-floor screen and signature verification, and then cause FIFO eviction of legitimately funded transactions' already-verified batches sitting ahead of them in the channel.

### Finding Description
The relevant call chain is:

1. `SigVerifyWorkerPool::worker_iteration` → `run_transaction_task` (`core/src/sigverify.rs:266-375`).
2. Inside `run_transaction_task`, packets are deduped, then (if saturated) filtered by `apply_priority_floor_to_batch` using `calculate_priority_from_bytes` (`core/src/sigverify.rs:302-324`, `core/src/transaction_priority.rs:73-88`). This computes priority as `reward / (1 + cost)` from the transaction's declared `compute_unit_price` and base fee — it never touches account balances, so a transaction signed by a brand-new, zero-lamport keypair with `ComputeBudgetInstruction::set_compute_unit_price(u64::MAX)` computes an arbitrarily high "priority" and sails through the floor check even during saturation.
3. `sigverify::ed25519_verify_serial` (`perf/src/sigverify.rs:127-133`) only checks the cryptographic signature — it has no concept of funding, so a self-signed, unfunded transaction passes as "valid."
4. The verified batch is handed to `state.banking_stage_sender.send(...)` (`core/src/sigverify.rs:353-369`), which is a `TracedSender` wrapping `EvictingSender<BankingPacketBatch>` (`core/src/banking_trace.rs:391-428`, `streamer/src/evicting_sender.rs:37-66`).
5. `EvictingSender::try_send` (`streamer/src/evicting_sender.rs:41-66`) evicts the *oldest message currently in the channel* via `self.receiver.try_recv()` whenever `sender.try_send` returns `Full`. This is strictly FIFO/age-based — the evicted item is not chosen based on fee, priority, or any per-transaction attribute, because `BankingPacketBatch` carries no priority metadata at this layer.

Because the priority floor gate is fee-blind to actual solvency, and the channel's overflow policy is purely FIFO, an attacker's high-rate unfunded traffic (already paying the same sigverify CPU cost as legitimate traffic) can occupy channel slots and evict older, legitimately funded/verified batches purely because they arrived earlier — independent of the relative economic value of either stream. This is architecturally distinct from the banking stage's own internal transaction container, which *does* evict by priority (`container.get_min_max_priority()`, referenced in `core/src/banking_stage/transaction_scheduler/scheduler_controller.rs:333-352`) — that fee-aware protection exists one layer downstream of the vulnerable `EvictingSender`, so it cannot compensate for batches dropped before ever reaching the container.

### Impact Explanation
This is a QoS/fee-prioritization evasion: sigverify CPU (a scarce, shared per-batch resource) is spent equally verifying attacker's unfunded packets and legitimate fee-paying packets, and the outcome of who reaches banking stage is decided by network-race/arrival-order rather than fee, violating the expected invariant that Agave's ingress path prioritizes fee-paying work. The scoped impact is legitimate paying transactions being silently and non-deterministically dropped post-verification under attacker-controlled timing, while CPU already spent verifying both streams is wasted for the loser. This matches the "grossly underpriced pre-fee work / QoS evasion" bounty category since the attacker pays nothing (no real SOL, no stake) to exert this influence, and does not require guessed/staked/whitelisted access — only ordinary unstaked TPU packet submission.

### Likelihood Explanation
- Preconditions: an unprivileged remote client can open a QUIC/UDP connection to a leader's public TPU port and submit arbitrary packets — this matches the assumed attacker model exactly.
- Constructing an unfunded, valid-signature, max-declared-priority transaction requires only local keypair generation and adding a `ComputeBudgetInstruction::set_compute_unit_price` instruction — trivially cheap and requires no real assets.
- Sustained high-rate submission is bandwidth-bound only, well within reach of a single unstaked network client; no rate-limiter in this path is fee- or funding-aware (the priority floor only screens by declared price).
- The eviction path is triggered any time `NON_VOTE_CHANNEL_CAPACITY` (`core/src/banking_trace.rs:29-31`, 16384) batches accumulate faster than banking stage drains them — a realistic condition under sustained burst load, which is precisely the "maximally-sized packet batches at sustained high rate" scenario in the prompt.
- This is repeatable indefinitely as long as the attacker sustains the send rate; no per-IP/per-connection accounting ties eviction weight back to solvency.

### Recommendation
Make the sigverify→banking-stage channel fee/priority-aware instead of purely FIFO:
- Either compute and attach a priority tag to `BankingPacketBatch` and change `EvictingSender` (or a variant used specifically here) to evict the lowest-priority batch rather than the oldest, or
- Strengthen the pre-verification `SchedulerPriorityFloor` gate to require a minimum feasible fee-payer balance check (or a cheap heuristic proxy) before admitting declared-priority transactions to bypass drop-under-saturation, so unfunded transactions cannot claim arbitrary priority for free.

### Proof of Concept
Integration test plan (extending `core/src/sigverify_stage.rs` test harness style, using `SigVerifyStage::new` wiring as in `test_sigverify_stage_tx_v1_feature_gate`):
1. Set up `SigVerifyStage` with a small `non_vote_sender`/`non_vote_receiver` `EvictingSender` channel capacity (e.g., via `BankingTracer::create_channel_non_vote` with an artificially small capacity for test purposes, or directly constructing an `EvictingSender::new_bounded(N)`).
2. Spawn a low-rate producer thread sending `M` distinct, funded, normally-priced transactions (valid signature, funded mint keypair, modest `compute_unit_price`) through the packet sender at a slow interval.
3. Concurrently spawn a high-rate producer thread sending a much larger volume of distinct, freshly-generated-unfunded-keypair transactions, each with `set_compute_unit_price(u64::MAX)` so they pass any active `SchedulerPriorityFloor`, submitted as fast as possible.
4. Drain `non_vote_receiver` slowly (simulating banking stage backlog) and track, by transaction signature, which of the `M` funded transactions' batches are ever received vs. never received (evicted).
5. Assert: with the attacker stream active, eviction rate for the funded stream is non-trivial (`> 0`, ideally comparable to its share of channel occupancy) — demonstrating that `EvictingSender`'s FIFO policy discards legitimate transactions in favor of unfunded attacker traffic irrespective of fee, and increasing attacker volume increases the funded stream's eviction count rather than only the attacker's own. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** core/src/transaction_priority.rs (L73-88)
```rust
pub(crate) fn calculate_priority_from_bytes(bank: &Bank, data: &[u8]) -> Option<u64> {
    let view = SanitizedTransactionView::try_new_sanitized(data, &sanitize_config()).ok()?;
    let runtime_tx = RuntimeTransaction::<SanitizedTransactionView<_>>::try_new(
        view,
        MessageHash::Compute,
        None,
    )
    .ok()?;
    let transaction_configuration = runtime_tx
        .transaction_configuration(&bank.feature_set)
        .ok()?;
    let (priority, _cost) =
        calculate_priority_and_cost(bank, &runtime_tx, &transaction_configuration);

    Some(priority)
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
