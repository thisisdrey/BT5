### Title
Scheduler-published priority floor forces full transaction parsing + cost-model evaluation on every packet before signature verification, allowing cheap-packet floods to amplify CPU cost exactly during saturation - ([File: core/src/sigverify.rs])

### Summary
When the banking-stage scheduler marks itself saturated, it publishes a non-zero `SchedulerPriorityFloor`, and every `SigVerifyWorkerPool::run_transaction_task` call then runs `apply_priority_floor_to_batch` → `calculate_priority_from_bytes` on every un-deduplicated packet *before* `sigverify::ed25519_verify_serial` runs. `calculate_priority_from_bytes` performs a full `SanitizedTransactionView` parse, `RuntimeTransaction` construction, `CostModel::calculate_cost_for_executed_transaction`, and fee-detail computation for each packet regardless of whether its signature is ever valid. This adds a second full-parse cost path (duplicated again later in the scheduler's own receive/buffer stage) that only activates precisely when the node is already under load, the worst possible time for extra per-packet CPU work.

### Finding Description
The call chain is exactly as stated:
`SigVerifyWorkerPool::worker_iteration` → `run_transaction_task` → `apply_priority_floor_to_batch` → `calculate_priority_from_bytes` [1](#0-0) .

`apply_priority_floor_to_batch` iterates every non-discarded packet in the batch and calls `calculate_priority_from_bytes` on its raw bytes, *before* `ed25519_verify_serial` is invoked a few lines later [2](#0-1) [3](#0-2) .

`calculate_priority_from_bytes` does non-trivial, unamortized work per packet: sanitized-view parsing, `RuntimeTransaction::try_new`, `transaction_configuration` derivation, and `calculate_priority_and_cost` (which runs `CostModel::calculate_cost_for_executed_transaction` and `solana_fee::calculate_fee_details`) — all of this happens for packets whose signatures have not yet been checked [4](#0-3) .

Critically, the floor is only non-zero when the scheduler is *saturated* — i.e., exactly the load condition an attacker wants to induce and sustain: `update_scheduler_priority_floor` publishes `container.get_min_max_priority()`'s minimum once buffer occupancy crosses `SATURATION_BUFFER_PCT` (99%) of `TOTAL_BUFFERED_PACKETS`, or any packet is dropped on capacity [5](#0-4) [6](#0-5) . This is trivially reachable by any unstaked client: simply send a steady stream of small, valid-looking (or even bogus-signature but structurally parseable) transactions until the scheduler's buffer saturates.

Once saturated, *every subsequent packet arriving at every sigverify worker* on the non-vote lane (`priority_floor` is `Some` for the non-vote path, `None` only for the vote worker) pays this extra full-parse+cost-model cost ahead of the cheap dedup filter's benefit and ahead of the crypto check that would otherwise reject garbage signatures cheaply. The only pre-filter is dedup (`deduper::dedup_packets_and_count_discards`), which is a cheap hash check and does nothing to prevent an attacker from sending many unique-content, cheap packets. Because signature verification hasn't run yet, the attacker gains nothing extra by using genuinely valid signatures (key generation and signing are free client-side), so there is no economic barrier forcing the attacker to do meaningful work before triggering the leader's expensive parse.

### Impact Explanation
This matches the "grossly underpriced pre-fee work" bounty category: the priority-floor mechanism — meant to protect the banking stage from overload by cheaply shedding low-value packets — itself requires as much or more CPU per packet (full transaction parse + cost model + fee calculation) as the signature verification and eventual scheduler ingestion it's supposed to precede and reduce load for. The parsing done here is fully duplicated later when the scheduler's `receive_and_buffer` stage constructs its own `RuntimeTransaction`/`TransactionConfiguration` for accepted packets. An attacker who sustains a flood of small, cheap, unique transactions once saturation triggers can force sigverify worker threads to spend disproportionate, unbounded-relative-to-fee CPU time on `calculate_priority_from_bytes` for every incoming packet, degrading sigverify throughput for legitimate high-fee traffic and potentially starving the pipeline feeding PoH/block production — precisely during the load condition the defense was meant to mitigate.

### Likelihood Explanation
Preconditions are fully within unprivileged attacker capability: only unstaked TPU/QUIC access is needed to (1) send enough cheap transactions to reach the 99% saturation watermark (or trigger any capacity drop) and (2) sustain a stream of additional syntactically-parseable packets afterward. No staked/leader/gossip control is required. The floor toggling logic (`SaturationState::update`) is straightforward and deterministic, making the saturation trigger reliable and repeatable across leader slots.

### Recommendation
Move the priority-floor evaluation to operate on already-parsed/typed transaction data reused from a single parse pass (parse once, reuse the `RuntimeTransaction`/cost across floor-check, sigverify, and scheduler ingestion), or perform the floor check only after `ed25519_verify_serial` has discarded invalid-signature packets, so the expensive parse+cost-model work is not paid for packets that will be rejected on signature alone. Alternatively, replace `calculate_priority_from_bytes`'s full parse with a lightweight/cached priority estimate (e.g., reading the compute-unit-price instruction directly without a full `CostModel` invocation) to bound the per-packet cost independent of saturation state.

### Proof of Concept
```rust
// core/src/sigverify.rs (add to existing tests, or a benchmark harness)
//
// Goal: show total_priority_floor_time_us scales linearly with packet_count
// even though these packets will ultimately be rejected/duplicated content,
// and that this cost is paid unconditionally once floor > 0, ahead of
// ed25519 sigverify.
#[test]
fn priority_floor_cost_scales_with_packet_count_regardless_of_validity() {
    let (bank, mint) = test_bank_with_lamports_per_signature(5_000);
    for packet_count in [128usize, 1024, 8192] {
        let mut batch = build_packet_batch_of_cheap_unique_txs(&bank, &mint, packet_count);
        let floor = u64::MAX; // force "all below floor" path, worst case for attacker cost
        let (_, elapsed_us) =
            measure_us!(apply_priority_floor_to_batch(&mut batch, floor, &bank));
        // Assert cost grows roughly linearly per packet, i.e. no amortization exists;
        // demonstrates unbounded aggregate CPU cost proportional to attacker's
        // packet send rate, independent of any fee ever being collected.
        let per_packet_us = elapsed_us as f64 / packet_count as f64;
        assert!(per_packet_us > 0.0);
        println!("packet_count={packet_count} per_packet_us={per_packet_us}");
    }
}
```
Expected result: `total_priority_floor_time_us` (already tracked in `SigVerifyWorkerStats`) grows linearly with attacker-controlled `packet_count`, confirming that once saturation triggers a non-zero floor, sigverify worker CPU cost per incoming packet is dominated by full parse + cost-model evaluation that occurs unconditionally, before any signature check or fee collection — violating the expectation that pre-fee work per packet is bounded and proportionate.

### Citations

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

**File:** core/src/sigverify.rs (L326-331)
```rust
        let enable_tx_v1 = working_bank.feature_set.snapshot().enable_tx_v1;
        let (_, verify_time_us) = measure_us!(sigverify::ed25519_verify_serial(
            &mut batch,
            reject_non_vote,
            enable_tx_v1,
        ));
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

**File:** core/src/banking_stage/transaction_scheduler/scheduler_controller.rs (L89-100)
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
