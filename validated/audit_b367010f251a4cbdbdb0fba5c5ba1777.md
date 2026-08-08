### Title
Unverified pre-signature-check priority spoofing lets an attacker inflate the sigverify priority floor and grief legitimate transactions - (File: `core/src/transaction_priority.rs`, `core/src/sigverify.rs`, `core/src/banking_stage/transaction_scheduler/scheduler_controller.rs`)

### Summary
The banking-stage scheduler publishes a `SchedulerPriorityFloor` once its transaction buffer saturates, and sigverify workers drop any packet whose priority is at-or-below that floor *before* signature verification, using a priority computed from raw, unverified packet bytes via `calculate_priority_from_bytes`. Because this priority is derived from unverified data, an unprivileged network sender can flood the buffer with cheap, ultimately-invalid packets that self-report arbitrarily high priority, driving the floor up and causing legitimate lower/mid-fee transactions to be discarded pre-verification at effectively zero cost to the attacker. This mirrors the referenced Uniswap report's root cause: a strict, hard gate is derived from attacker-influenceable state rather than from parameters/guarantees the caller actually pays for, enabling a griefing DoS on other users' otherwise-valid work.

### Finding Description
`SchedulerPriorityFloor` is shared between the banking-stage scheduler and sigverify workers [1](#0-0) . The scheduler computes and publishes the floor once its retained buffer nears capacity (`SATURATION_BUFFER_PCT = 99%`), setting the floor to the current minimum priority held in the buffer [2](#0-1) .

In sigverify, `run_transaction_task` reads this floor and calls `apply_priority_floor_to_batch`, which drops (marks discard) any packet whose priority is at-or-below the floor — and this happens *before* `ed25519_verify_serial` runs [3](#0-2) [4](#0-3) .

The priority used for this pre-verification gate is computed straight from raw packet bytes via `calculate_priority_from_bytes`, which only requires the bytes to parse as a `SanitizedTransactionView` — it never checks the transaction's signature validity [5](#0-4) . The priority formula `P = R / (1 + C)` derives its reward `R` from the transaction's self-declared prioritization fee (`compute_unit_price`) [6](#0-5) . Because none of the payer's balance, signature, or actual fee payment is checked at this stage, an attacker can construct a well-formed-but-garbage transaction (fake signature, non-existent/underfunded payer) with a maximal `compute_unit_price`, producing an arbitrarily high computed priority for free.

By flooding the scheduler's buffer with such packets to reach saturation, the attacker pushes the published floor to a high value. Every genuine transaction whose priority is at or below that floor is then discarded by sigverify before its signature is even checked, denying it any chance to compete on the merits of its actual fee once verified — a pure griefing vector exploiting the same "use of unverified/attacker-controllable state to gate legitimate work" pattern as the referenced Uniswap slippage bug (there, unauthenticated balance manipulation; here, unauthenticated priority-byte manipulation).

### Impact Explanation
This is reachable by any unprivileged network sender submitting packets to the public TPU (no stake or signature validity required to influence the computed priority), fitting the "QUIC/UDP streamer, packet dedup and sigverify" scope. The impact is QoS evasion / griefing: honest, adequately-priced (or even well-priced) transactions can be discarded pre-verification purely because an attacker fabricated higher-looking-but-worthless transactions, at negligible attacker cost (garbage transactions cost nothing since they fail signature checks and are simply dropped, never landing or paying fees). This can suppress legitimate user transactions during periods when the buffer is saturated — exactly the periods when transaction throughput/fairness matters most.

### Likelihood Explanation
Likelihood is high: the mechanism requires only sending crafted, parseable-but-invalid transaction bytes with an inflated `compute_unit_price` field over the TPU, no stake, and no valid signature. Saturation (buffer >= 99% full) is a state that any high-throughput leader can reach under normal load, and an attacker can help trigger/sustain it by contributing volume.

### Recommendation
Do not gate against an unverified, self-reported priority value. Options:
- Require the priority-floor comparison to use a priority computed only after (or alongside) minimal proof of payer solvency/signature validity, or
- Rate-limit/cost-bound the influence any single unverified packet can have on the floor (e.g., cap contributions from not-yet-verified packets, or compute the floor only from transactions that have already passed sigverify/cost checks), or
- Require some minimal signature verification before a packet's declared priority is allowed to affect the shared floor used to drop other users' packets.

### Proof of Concept
1. Attacker crafts N packets that parse successfully as `SanitizedTransactionView` (satisfy `sanitize_config()`), each with `ComputeBudgetInstruction::set_compute_unit_price(u64::MAX)` in the message, using arbitrary (not necessarily correctly signed, non-existent payer) keys — see the test helper pattern in `core/src/transaction_priority.rs::make_tx_bytes` demonstrating how `compute_unit_price` directly controls `calculate_priority_from_bytes` output [7](#0-6) .
2. Attacker floods the TPU with enough such packets (and/or during natural high load) to push `SchedulerController`'s buffer to the `SATURATION_BUFFER_PCT` watermark, causing `update_scheduler_priority_floor` to publish a high floor derived from the attacker's fabricated high-priority packets sitting in the buffer [2](#0-1) .
3. Concurrently, sigverify workers apply this floor to all newly arriving packets in `apply_priority_floor_to_batch` before verifying any signature, discarding legitimate transactions whose real (paid-for) priority is below the attacker-inflated floor [4](#0-3) .
4. The attacker's own packets are eventually rejected downstream (invalid signature/insufficient funds) and never cost the attacker anything, while legitimate users' transactions were already dropped and must retry — a griefing denial of service on ordinary transaction submission.

### Citations

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

**File:** core/src/transaction_priority.rs (L14-66)
```rust
/// Calculate priority and cost for a transaction:
///
/// Cost is calculated through the `CostModel`,
/// and priority is calculated through a formula here that attempts to sell
/// blockspace to the highest bidder.
///
/// The priority is calculated as:
/// P = R / (1 + C)
/// where P is the priority, R is the reward,
/// and C is the cost towards block-limits.
///
/// Current minimum costs are on the order of several hundred,
/// so the denominator is effectively C, and the +1 is simply
/// to avoid any division by zero due to a bug - these costs
/// are calculated by the cost-model and are not direct
/// from user input. They should never be zero.
/// Any difference in the prioritization is negligible for
/// the current transaction costs.
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

**File:** core/src/transaction_priority.rs (L124-165)
```rust
    fn make_tx_bytes(mint: &Keypair, recent_blockhash: Hash, compute_unit_price: u64) -> Vec<u8> {
        let to = Pubkey::new_unique();
        let transfer = system_instruction::transfer(&mint.pubkey(), &to, 1);
        let prioritization = ComputeBudgetInstruction::set_compute_unit_price(compute_unit_price);
        let message = Message::new(&[transfer, prioritization], Some(&mint.pubkey()));
        let tx = Transaction::new(&[mint], message, recent_blockhash);
        bincode::serialize(&VersionedTransaction::from(tx)).unwrap()
    }

    fn priority_from(bank: &Bank, bytes: &[u8]) -> u64 {
        calculate_priority_from_bytes(bank, bytes).unwrap()
    }

    #[test]
    fn priority_from_bytes_returns_none_for_garbage() {
        let (bank, _) = test_bank();
        assert!(calculate_priority_from_bytes(&bank, &[]).is_none());
        assert!(calculate_priority_from_bytes(&bank, &[0u8; 32]).is_none());
    }

    #[test]
    fn priority_is_zero_when_base_and_priority_fees_are_zero() {
        // Test bank has lamports_per_signature = 0, so base fee is 0.
        // With compute_unit_price = 0, priority fee is also 0 → reward 0 → priority 0.
        let (bank, mint) = test_bank();
        assert_eq!(bank.fee_structure().lamports_per_signature, 0);
        let bytes = make_tx_bytes(&mint, bank.last_blockhash(), 0);
        assert_eq!(priority_from(&bank, &bytes), 0);
    }

    #[test]
    fn higher_compute_unit_price_yields_higher_priority() {
        // Need non-zero base fee, otherwise the reward short-circuits to 0
        // and all priorities collapse regardless of compute_unit_price.
        let (bank, mint) = test_bank_with_lamports_per_signature(5_000);
        let low = priority_from(&bank, &make_tx_bytes(&mint, bank.last_blockhash(), 1));
        let high = priority_from(
            &bank,
            &make_tx_bytes(&mint, bank.last_blockhash(), 1_000_000),
        );
        assert!(high > low, "expected high {high} > low {low}");
    }
```
