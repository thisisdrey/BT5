Confirmed: `apply_priority_floor_to_batch` runs entirely before `sigverify::ed25519_verify_serial` at [1](#0-0) , and it computes each packet's priority from unauthenticated wire bytes via `calculate_priority_from_bytes`, which trusts the packet's self-declared `ComputeBudgetInstruction::set_compute_unit_price`/limit values with no signature check yet [2](#0-1) [3](#0-2) .

### Title
Priority-floor pre-filter in sigverify trusts unauthenticated, self-declared fee fields, allowing forged high-priority garbage to evade the cheap early-drop and force full sigverify CPU cost under saturation - (File: core/src/sigverify.rs)

### Summary
This is analogous to the IchiLpOracle bug class: a security-relevant decision is made by trusting an instantaneous, cheaply-manipulable value supplied by the party being judged, instead of a verified/robust one. In Uniswap's IchiVault, `slot0` (spot price) is trusted before any manipulation-resistance check. In Agave's sigverify path, the "priority floor" admission decision is computed from a packet's raw, **unsigned** bytes before Ed25519 verification ever runs, so the "priority" score is entirely attacker-declared and unauthenticated at the moment it's used to gate expensive downstream work.

### Finding Description
When banking-stage saturates, `SchedulerController`'s `SaturationState::publish_floor` writes the queue-min priority into a shared `SchedulerPriorityFloor` [4](#0-3) . Sigverify workers then call `apply_priority_floor_to_batch`, which parses each still-unverified packet and calls `calculate_priority_from_bytes(bank, data)` to decide whether to mark it `discard` (dropped before ed25519 verification) or keep it [3](#0-2) .

`calculate_priority_from_bytes` builds a `SanitizedTransactionView` straight from the raw packet bytes and reads `transaction_configuration.priority_fee_lamports` (derived directly from the `ComputeBudgetInstruction::set_compute_unit_price`/`set_compute_unit_limit` fields embedded in the message) to compute a reward-over-cost priority score [5](#0-4) . Crucially, this happens in `run_transaction_task` strictly before `sigverify::ed25519_verify_serial` runs [1](#0-0) . At this point, nothing about the packet has been authenticated: the signature field can be arbitrary garbage, the fee-payer need not exist, and no `check_fee_payer_unlocked`/account-lock validation (as done later in `receive_and_buffer.rs`) has occurred.

The entire purpose of the priority floor, per its own doc comment, is to let sigverify "drop at-or-below-floor arrivals" *before* paying the CPU cost of signature verification, precisely under saturation when CPU is scarce [6](#0-5) . But because the score used for that decision is taken from unauthenticated attacker-controlled bytes, any attacker can trivially stamp an arbitrarily high `compute_unit_price` onto garbage/invalid-signature packets to guarantee they clear the floor check and proceed to the expensive `ed25519_verify_serial` path, while genuinely fee-paying-but-lower-priority real user transactions get dropped by the same floor.

### Impact Explanation
This defeats the stated purpose of the priority-floor optimization: it's supposed to save CPU under saturation by cheaply discarding low-value traffic before the costly cryptographic verification step. Since the discriminator is unauthenticated, an attacker can force the node to spend full sigverify CPU on cost-free forged packets that will fail signature verification anyway, while simultaneously starving legitimate lower (but real, fee-paying) priority transactions that get pre-emptively dropped by the same floor logic. This is a QoS-evasion / underpriced-work class issue: the "protection" mechanism can be bypassed for free by the very class of traffic (spam) it exists to filter, precisely during the saturation condition where the mechanism matters most.

### Likelihood Explanation
Likelihood is high given saturation conditions are the intended (and expected) operating point for this mechanism to trigger, and any unprivileged network sender can submit packets to the TPU with fabricated `ComputeBudgetInstruction` fields and an invalid/garbage signature at zero cost (no real funds or valid keypair required, since verification hasn't happened yet). No special privileges are required — this is exactly the "unprivileged-user reachable" QUIC/streamer → sigverify path the scan targets.

### Recommendation
Do not rely on unauthenticated, self-declared fee/priority fields to gate work ahead of signature verification. Options: (1) perform Ed25519 verification (or at least a cheap secondary integrity check) before or as part of the priority-floor decision, rather than after; (2) bound the trust placed in pre-verification priority to a much lower-cost heuristic (e.g., packet size/dedup only) rather than a fee-derived value that's fully attacker-controlled; or (3) after verification, re-check computed priority against the floor and discard verified-but-below-floor transactions rather than only filtering before verification.

### Proof of Concept
1. Craft a raw transaction-shaped packet with a `ComputeBudgetInstruction::set_compute_unit_limit` set to the minimum and `set_compute_unit_price` set to `u64::MAX` (or otherwise maximal), and an arbitrary/garbage 64-byte signature and arbitrary fee-payer pubkey — no valid keypair or funded account needed.
2. Send a flood of such packets to the TPU while the leader's banking-stage scheduler is saturated (so `SchedulerPriorityFloor::get()` returns non-zero, per `scheduler_controller.rs`).
3. In `run_transaction_task`, `apply_priority_floor_to_batch` computes `calculate_priority_from_bytes` on these packets using only the forged, unauthenticated `priority_fee_lamports`/cost fields [2](#0-1) ; because the declared price is maximal, `priority` exceeds `floor`, so `packet.meta_mut().set_discard(true)` is never invoked for these packets and they proceed to `ed25519_verify_serial` [7](#0-6) .
4. Meanwhile, concurrently arriving legitimate low-priority (but validly signed and fee-paying) transactions with real compute-unit prices below the floor are silently discarded at line `packet.meta_mut().set_discard(true)` [7](#0-6) .
5. Result: the node burns full CPU cycles on `ed25519_verify_serial` for the attacker's zero-cost garbage while dropping real user traffic that the floor was meant to protect against — the exact "underpriced pre-fee work"/QoS-evasion pattern.

### Citations

**File:** core/src/sigverify.rs (L300-331)
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

        let enable_tx_v1 = working_bank.feature_set.snapshot().enable_tx_v1;
        let (_, verify_time_us) = measure_us!(sigverify::ed25519_verify_serial(
            &mut batch,
            reject_non_vote,
            enable_tx_v1,
        ));
```

**File:** core/src/sigverify.rs (L413-440)
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
}
```

**File:** core/src/transaction_priority.rs (L32-88)
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

/// Evaluate raw packet bytes against the pf-floor, returning the computed
/// priority.
///
/// Returns `None` if the bytes don't parse as a valid transaction, in which
/// case the caller should leave the packet to downstream stages to reject.
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
