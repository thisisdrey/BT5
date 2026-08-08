### Title
Priority floor is trivially gameable by self-declared fee, letting unpaid garbage-signature packets bypass the pre-sigverify cost gate - ([File: core/src/sigverify.rs])

### Summary
`apply_priority_floor_to_batch` decides whether to drop a packet before the CPU-expensive `ed25519_verify_serial` step purely from a self-declared, unverified priority value computed by `calculate_priority_from_bytes`. Because this priority is derived from attacker-controlled `ComputeBudgetInstruction::SetComputeUnitPrice` bytes and never checks the fee payer's actual ability to pay (which is only checked much later, after sigverify/scheduling), an unstaked attacker can craft syntactically-valid but garbage-signature transactions with an inflated compute-unit-price to always clear the floor and force full ed25519 signature verification for free.

### Finding Description
In `SigVerifyWorkerPool::run_transaction_task` (`core/src/sigverify.rs:266-375`), the sequence is: dedup → (if `SchedulerPriorityFloor` > 0) `apply_priority_floor_to_batch` → `ed25519_verify_serial`. The floor check at `core/src/sigverify.rs:413-440` calls `calculate_priority_from_bytes` (`core/src/transaction_priority.rs:73-88`), which only does structural parsing (`SanitizedTransactionView::try_new_sanitized` + `RuntimeTransaction::try_new`) and computes a priority via `calculate_priority_and_cost` (`core/src/transaction_priority.rs:32-66`). That priority is `reward / (1 + cost)`, where `reward` is derived solely from the transaction's *declared* signature count and declared `priority_fee_lamports` (the compute-unit price the attacker puts in the message) — none of which requires a valid signature or a real, funded fee payer account.

Because the attacker fully controls the `compute_unit_price` field, they can set it arbitrarily high to produce a priority that exceeds any real, bounded `SchedulerPriorityFloor` value (which is capped by whatever legitimate transactions in the scheduler's buffer are offering). The packet is therefore kept (`any_kept = true`) rather than discarded, and the worker proceeds unconditionally to `sigverify::ed25519_verify_serial` (`core/src/sigverify.rs:326-331`), the CPU-heavy elliptic-curve verification step — even though the packet's signature is garbage and will fail verification, meaning no fee is ever, or could ever be, collected.

The floor mechanism's own doc comment states "In practice, transactions always have non-zero priorities," implicitly assuming priorities reflect genuine fee-paying transactions; it provides no adversarial guarantee that a declared priority corresponds to actual payment capability.

### Impact Explanation
This lets an unstaked remote attacker force the leader's sigverify worker pool to spend full ed25519 verification CPU on packets that pay zero fee, specifically during the precondition the floor mechanism was built to protect against (scheduler saturation). This degrades real transaction throughput at exactly the time capacity is most contended — a grossly underpriced pre-fee work / QoS-evasion condition against the sigverify stage's CPU budget, scoped to `core/src/sigverify.rs`'s `apply_priority_floor_to_batch` / `run_transaction_task`.

### Likelihood Explanation
Requires only: (1) the scheduler buffer be saturated (a realistic, attacker-triggerable-adjacent, or naturally-occurring condition under load, publishing a non-zero `SchedulerPriorityFloor` via `SaturationState`/`update_scheduler_priority_floor` in `scheduler_controller.rs:333-352`), and (2) the attacker send TPU packets with a syntactically valid transaction shape and an inflated `SetComputeUnitPrice` instruction plus arbitrary/garbage signature bytes. No staked identity, valid keys, or account balance is needed to construct such packets — this is fully within the unprivileged remote-attacker capability described in scope.

### Recommendation
Do not let the sigverify-side priority floor trust self-declared fee fields alone. Options: bound the maximum credit a declared-but-unverified fee can contribute to priority (e.g., cap or discount `priority_fee_lamports` used in the floor comparison), require a cheap but adversarially-meaningful check (e.g., a lightweight partial signature/format sanity check, or per-connection/IP rate limiting keyed independent of claimed priority) before the floor can be satisfied, or move the floor check after a cheap batched pre-verification pass so unpaid/garbage-signature packets are dropped regardless of claimed priority.

### Proof of Concept
Rust unit test in `core/src/sigverify.rs` (or `transaction_priority.rs`) test module:
1. Build a bank with non-zero `lamports_per_signature`.
2. Craft a `VersionedTransaction` with `ComputeBudgetInstruction::set_compute_unit_price(u64::MAX / 2)`, a valid structural message/signature-count, but with the signature bytes overwritten with garbage (so `ed25519_verify_serial`/`verify_packet` will reject it).
3. Set `SchedulerPriorityFloor::set(floor)` to a realistic legitimate value (e.g., priority of a normal transfer transaction with default fee).
4. Call `apply_priority_floor_to_batch(&mut batch, floor, &bank)` and assert `dropped == 0` and `all_below == false` (the garbage-signature/unpaid packet is kept).
5. Call `sigverify::ed25519_verify_serial` on the batch and assert the packet ends up `discard()==true` (signature invalid, so it will never reach banking/consume a fee), demonstrating verification work was spent on an unpaid packet that the floor should have been able to reject cheaply.
6. Optionally, benchmark CPU time of step 4 (cheap) vs. step 5 (ed25519 verify, expensive) across N such crafted packets to quantify the disproportionate cost, comparing against a baseline of packets that fail structural parsing entirely (which are dropped even earlier, at `discard_or_dedup_fail`/`data(..)` checks, without reaching `ed25519_verify_serial`). [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** core/src/transaction_priority.rs (L60-88)
```rust
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

**File:** perf/src/sigverify.rs (L108-133)
```rust
pub fn ed25519_verify(
    thread_pool: &rayon::ThreadPool,
    batches: &mut [PacketBatch],
    reject_non_vote: bool,
    packet_count: usize,
    enable_tx_v1: bool,
) {
    debug!("CPU ECDSA for {packet_count}");
    thread_pool.install(|| {
        batches.par_iter_mut().flatten().for_each(|mut packet| {
            if !packet.meta().discard()
                && !verify_packet(&mut packet, reject_non_vote, enable_tx_v1)
            {
                packet.meta_mut().set_discard(true);
            }
        });
    });
}

pub fn ed25519_verify_serial(batch: &mut PacketBatch, reject_non_vote: bool, enable_tx_v1: bool) {
    for mut packet in batch.iter_mut() {
        if !packet.meta().discard() && !verify_packet(&mut packet, reject_non_vote, enable_tx_v1) {
            packet.meta_mut().set_discard(true);
        }
    }
}
```
