### Title
Priority-floor bypass via unauthenticated priority field forces full ed25519 verification on garbage-signature packets - ([File: core/src/sigverify.rs])

### Summary
`SigVerifyWorkerPool::run_transaction_task` calls `apply_priority_floor_to_batch`, which derives a packet's priority via `calculate_priority_from_bytes` before any signature check is performed. Because that priority is computed purely from attacker-controlled message bytes (compute-unit price/limit) with no signature or balance verification, an unstaked attacker can trivially inflate the declared priority to defeat the floor and force every garbage-signature packet through the expensive `ed25519_verify_serial` path even while the scheduler is saturated.

### Finding Description
In `core/src/sigverify.rs::run_transaction_task`, once dedup passes, the worker checks the scheduler-published `SchedulerPriorityFloor`: [1](#0-0) 

`apply_priority_floor_to_batch` drops a packet only if its computed priority is `<= floor`; anything above (including unparseable-but-`Some` results) is kept and proceeds unconditionally to `sigverify::ed25519_verify_serial`: [2](#0-1) [3](#0-2) 

The priority itself comes from `calculate_priority_from_bytes`, which only parses the wire format (`SanitizedTransactionView::try_new_sanitized`) and computes a fee/cost-derived priority — it never validates the signature or checks that the fee payer can pay: [4](#0-3) 

The priority formula is `reward * MULTIPLIER / (cost + 1)`, computed purely from the declared `compute_unit_price`/`compute_unit_limit` fee-structure inputs via `saturating_mul`/`saturating_div`, with no signature or balance dependency: [5](#0-4) 

Because the attacker fully controls the message bytes and needs no valid signature or funded account, they can set `compute_unit_price` arbitrarily high to make `calculate_priority_from_bytes` return a value that exceeds any realistic `SchedulerPriorityFloor` (which is bounded by the actual min priority of legitimately queued transactions). This guarantees `apply_priority_floor_to_batch` never drops the packet, so it falls through to full `ed25519_verify_serial` — the CPU-heavy step the floor mechanism exists specifically to avoid under saturation — even though the packet's signature is garbage and will never pay a fee.

The floor mechanism's documented purpose is exactly to shed low-value packets before the expensive verification step when the scheduler buffer is saturated: [6](#0-5) 
Since the "priority" input to that gate is unauthenticated and free to fabricate, the gate provides no actual protection against unsigned/garbage packets — it only filters honest but low-fee submitters, while a malicious flood of inflated-priority garbage bypasses it for free.

### Impact Explanation
This falls under sigverify CPU exhaustion delaying legitimate transaction processing/block production, matching the scoped impact. During scheduler saturation — the precise condition meant to activate load-shedding — an attacker can still force the sigverify worker pool to spend full `ed25519_verify_serial` CPU time on packets that were never going to pay a fee and never had a chance of being valid, defeating the load-shedding invariant ("work spent per packet before a fee is collected is bounded and proportionate; cheap packets cannot force expensive verification").

### Likelihood Explanation
Highly feasible: it requires only an unstaked client and `SchedulerPriorityFloor` configured above zero (which happens automatically whenever the scheduler's retained buffer nears capacity — i.e., exactly under load/attack conditions). Crafting a packet with an inflated `compute_unit_price` compute-budget instruction and a garbage signature is a single cheap serialization step, fully reproducible and repeatable at scale (no per-packet cost, no staking, no rate limit specific to this path beyond generic packet ingress limits).

### Recommendation
Do not let unauthenticated, attacker-declared fee/priority fields alone determine whether a packet skips signature verification. Options: (1) cap the maximum priority credited to bytes-derived (pre-verification) calculation to a sane bound independent of attacker-chosen `compute_unit_price`, (2) require a cheap-but-costly proof of validity (e.g., a lightweight signature-shape/format sanity check that rejects trivially-forged signatures before trusting the derived priority), or (3) rate-limit/cost-charge sigverify work per source IP/connection independent of declared priority so inflated-priority floods cannot fully evade the shedding logic during saturation.

### Proof of Concept
```rust
// core/src/transaction_priority.rs (or a new test module)
#[test]
fn inflated_compute_unit_price_bypasses_priority_floor_for_garbage_signature_tx() {
    let (bank, mint) = test_bank_with_lamports_per_signature(5_000);

    // Legitimate low-priority tx used to compute a realistic floor.
    let low_bytes = make_tx_bytes(&mint, bank.last_blockhash(), 1);
    let low_priority = calculate_priority_from_bytes(&bank, &low_bytes).unwrap();

    // Attacker-crafted tx: valid wire *shape* (parses via SanitizedTransactionView),
    // but signature bytes are garbage/unsigned, and compute_unit_price is maxed out.
    let to = Pubkey::new_unique();
    let transfer = system_instruction::transfer(&mint.pubkey(), &to, 1);
    let prioritization = ComputeBudgetInstruction::set_compute_unit_price(u64::MAX);
    let message = Message::new(&[transfer, prioritization], Some(&mint.pubkey()));
    let mut tx = Transaction::new_unsigned(message);
    tx.signatures = vec![Signature::default(); 1]; // garbage/zero signature, never verifies
    let attacker_bytes = bincode::serialize(&VersionedTransaction::from(tx)).unwrap();

    let attacker_priority = calculate_priority_from_bytes(&bank, &attacker_bytes).unwrap();

    // Floor set from the legitimate low-priority tx (saturated scheduler state).
    let floor = low_priority;
    assert!(
        attacker_priority > floor,
        "garbage-signature packet with inflated declared priority ({attacker_priority}) \
         escapes the floor ({floor}) despite paying no fee and never verifying"
    );
    // => this packet will NOT be dropped by apply_priority_floor_to_batch and will
    // proceed unconditionally into ed25519_verify_serial, wasting full verify CPU
    // on a packet that will fail signature verification and pay zero fee.
}
```
Companion integration/bench test: extend `core/benches/sigverify_stage.rs` to send a batch of such inflated-priority/garbage-signature packets with `SchedulerPriorityFloor` set high (simulating saturation), and assert `total_dropped_below_priority_floor` remains 0 while `total_verify_time_us` for the batch is comparable to a batch of equally-sized legitimate packets — demonstrating the floor provides no CPU savings against this attacker input.

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

**File:** core/src/transaction_priority.rs (L68-88)
```rust
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
