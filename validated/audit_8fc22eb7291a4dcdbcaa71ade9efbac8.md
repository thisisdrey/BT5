### Title
Sigverify's priority-floor check parses and cost-models every packet before signature verification, letting unsigned garbage forcibly consume CPU once the scheduler is saturated - (File: core/src/sigverify.rs)

### Summary
`SigVerifyStage::run_transaction_task` invokes `apply_priority_floor_to_batch` before `sigverify::ed25519_verify_serial` runs, and that function calls `calculate_priority_from_bytes`, which fully parses each packet via `SanitizedTransactionView::try_new_sanitized` and runs `CostModel::estimate_cost`/`calculate_cost_for_executed_transaction` for every non-discarded, non-duplicate packet. Because this happens strictly before ed25519 signature verification, an unstaked remote client can force this parse+cost work on packets with completely invalid (random) signatures, as long as the packet passes deduplication (unique bytes) and the scheduler-published priority floor is non-zero (buffer saturation).

### Finding Description
The call chain is: `SigVerifyStage::run_transaction_task` (core/src/sigverify.rs) dedups the batch, then checks `state.priority_floor` and, if `floor > 0`, calls `apply_priority_floor_to_batch(&mut batch, floor, &working_bank)` [1](#0-0) . Only after that gate does the code call `sigverify::ed25519_verify_serial(&mut batch, ...)` [2](#0-1) .

`apply_priority_floor_to_batch` iterates every kept packet and calls `calculate_priority_from_bytes(bank, data)` [3](#0-2) . That function performs full transaction sanitization and cost estimation with no signature check: [4](#0-3) 

This means: (1) `SanitizedTransactionView::try_new_sanitized` parses the wire bytes and validates structural sanitization only (no signature check); (2) `RuntimeTransaction::try_new` and `transaction_configuration` resolve compute-budget instructions; (3) `calculate_priority_and_cost` invokes `CostModel::calculate_cost_for_executed_transaction`, which sums signature/write-lock/execution/data costs [5](#0-4) . None of this validates that the packet's signatures are cryptographically valid — signature verification only happens afterward, in `ed25519_verify_serial`.

The floor is only non-zero when the scheduler is saturated, published via `SaturationState::publish_floor` in `SchedulerController::update_scheduler_priority_floor` [6](#0-5) , and read by sigverify via the shared `SchedulerPriorityFloor` [7](#0-6) , wired in `core/src/tpu.rs` between `SigVerifyStage::new` and the scheduler [8](#0-7) . Saturation is buffer-size-driven, not fee-driven, so it can be reached by flooding with cheap/garbage-signature but byte-unique packets (dedup only rejects exact byte duplicates, and an attacker can trivially vary bytes e.g. via distinct nonces/blockhashes/memos) — so an unstaked attacker can both cause saturation and subsequently exploit the resulting floor check, all while never producing a single valid signature.

### Impact Explanation
Scoped impact: sigverify worker CPU is spent parsing and cost-modeling attacker packets that have zero valid signatures and pay zero fees, ahead of the actual signature check that would otherwise reject them cheaply. This degrades leader packet-processing throughput under saturation — exactly the condition the priority floor was meant to protect, but the floor check itself is the expensive step done pre-verification. This matches the "grossly underpriced pre-fee work" / QoS-evasion category described in the audit scope for `runtime/src/prioritization_fee.rs`-adjacent logic (mirrored here in `core/src/sigverify.rs` and `core/src/transaction_priority.rs`).

### Likelihood Explanation
Preconditions: the scheduler buffer must be saturated (achievable by flooding with byte-unique packets, which is cheap and repeatable for an unstaked client since dedup only rejects exact duplicates) and the attacker needs an open QUIC connection to the TPU port, which is available to any unstaked remote client. Once saturated, every subsequent unsigned/garbage-signature packet triggers the parse+cost work in `apply_priority_floor_to_batch` before verification, making this fully repeatable and proportional to attacker-controlled packet volume.

### Recommendation
Reorder or gate the priority-floor check to be cheaper or to run after a lightweight pre-check that doesn't require a full transaction-view parse and cost-model computation, or move the floor check to occur only for packets that have already passed (or are concurrently passing) signature verification, so unsigned packets cannot force CostModel execution. Alternatively, cache/reuse a cheap structural pre-check (e.g., minimal signature-count/format check) before invoking `calculate_priority_from_bytes`, and rate-limit the per-connection/per-IP packet rate before packets ever reach `apply_priority_floor_to_batch`.

### Proof of Concept
```rust
// core/src/sigverify.rs (test module)
#[test]
fn priority_floor_costs_scale_with_unsigned_packet_count() {
    use {
        solana_perf::packet::{Packet, PacketBatch},
        std::time::Instant,
    };

    let (bank, _mint) = test_bank(); // helper building a minimal Bank
    let floor = 1u64; // non-zero floor as under scheduler saturation

    for &n in &[100usize, 1000, 5000] {
        let mut batch = PacketBatch::with_capacity(n);
        for i in 0..n {
            // Build a structurally valid Message but with random 64-byte signatures.
            let bytes = make_tx_bytes_with_garbage_signature(i as u64);
            batch.push(Packet::from_bytes(&bytes));
        }

        let start = Instant::now();
        let (dropped, _all_below) = apply_priority_floor_to_batch(&mut batch, floor, &bank);
        let elapsed = start.elapsed();

        // Assert zero landed fee / zero valid signatures despite work done.
        assert_eq!(dropped_signatures_valid_count(&batch), 0);
        println!("n={n} elapsed={elapsed:?} dropped={dropped}");
        // Expect wall time to scale roughly linearly with n, demonstrating
        // unpriced pre-verification cost proportional to attacker packet count.
    }
}
```
Expected result: elapsed time in `apply_priority_floor_to_batch` grows linearly with the number of attacker-supplied unsigned packets, while `ed25519_verify_serial` (which would reject all of them) has not yet run and no fee has been collected — demonstrating unbounded, fee-free CPU consumption gated only by scheduler saturation, not by signature validity or fee payment.

### Citations

**File:** core/src/sigverify.rs (L300-323)
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

**File:** core/src/sigverify.rs (L413-438)
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
```

**File:** core/src/transaction_priority.rs (L32-66)
```rust
pub(crate) fn calculate_priority_and_cost<Tx: TransactionMeta + SVMStaticMessage>(
    bank: &Bank,
    transaction: &Tx,
    transaction_configuration: &TransactionConfiguration,
) -> (u64, u64) {
    let cost = CostModel::calculate_cost_for_executed_transaction(
        transaction,
        u64::from(transaction_configuration.compute_unit_limit),
        transaction_configuration.loaded_accounts_data_size_limit,
        &bank.feature_set,
    )
    .sum();
    let fee_details = solana_fee::calculate_fee_details(
        transaction,
        bank.fee_structure().lamports_per_signature,
        transaction_configuration.priority_fee_lamports,
        bank.fee_features(),
    );
    let reward = bank
        .calculate_reward_and_burn_fee_details(&CollectorFeeDetails::from(fee_details))
        .get_deposit();

    // We need a multiplier here to avoid rounding down too aggressively.
    // For many transactions, the cost will be greater than the fees in terms of raw lamports.
    // For the purposes of calculating prioritization, we multiply the fees by a large number so that
    // the cost is a small fraction.
    // An offset of 1 is used in the denominator to explicitly avoid division by zero.
    const MULTIPLIER: u64 = 1_000_000;
    (
        reward
            .saturating_mul(MULTIPLIER)
            .saturating_div(cost.saturating_add(1)),
        cost,
    )
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
