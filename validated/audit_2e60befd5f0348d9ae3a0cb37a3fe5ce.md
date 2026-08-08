Based on my investigation, the bitcoin-fee report itself (a wallet/client-side fee-estimation issue for a different chain) has no direct analog in agave's validator code. However, the report's underlying theme — logic that behaves worse specifically *during network congestion* — does map to a genuine issue in the sigverify/priority-floor path that is reachable by unprivileged, unauthenticated network peers.

### Title
Priority-floor filtering runs full transaction parsing and cost-model computation on unauthenticated packets before signature verification, amplifying CPU load exactly when the node is under congestion - (File: `core/src/sigverify.rs`)

### Summary
`SchedulerPriorityFloor` is a value the banking-stage scheduler publishes to sigverify workers "when saturated" (i.e., precisely during congestion). When the floor is non-zero, `SigVerifyWorkerPool::run_transaction_task` calls `apply_priority_floor_to_batch`, which fully parses and computes a cost/priority for every non-discarded, non-deduped packet in the batch — using `calculate_priority_from_bytes` → `calculate_priority_and_cost` → `CostModel::calculate_cost_for_executed_transaction` — **before** `sigverify::ed25519_verify_serial` runs.

### Finding Description
The relevant call ordering in `run_transaction_task` is: dedup → `apply_priority_floor_to_batch` (if `floor > 0`) → `ed25519_verify_serial`. [1](#0-0) 

`apply_priority_floor_to_batch` parses each packet's raw bytes into a `SanitizedTransactionView`/`RuntimeTransaction` and computes its cost via the cost model, all prior to any signature check on that data: [2](#0-1) 

The parsing/costing routine it calls is `calculate_priority_from_bytes`, which builds a full `RuntimeTransaction` and invokes `calculate_priority_and_cost`: [3](#0-2) [4](#0-3) 

`CostModel::calculate_cost_for_executed_transaction` iterates every instruction in the (unsigned/unverified) message and, for anything targeting the system program, performs a `limited_deserialize` on attacker-controlled instruction data to estimate allocation sizes: [5](#0-4) [6](#0-5) 

Critically, the floor is only non-zero when the scheduler has detected saturation: [7](#0-6) 

So the code path that performs the *most* per-packet parsing/deserialization/cost-model work is switched on precisely when the validator is already under load — the opposite of the intended fail-cheap-first design where the (comparatively cheap, batched) ed25519 check would normally reject garbage quickly. An unprivileged peer sending the QUIC/UDP streamer a stream of syntactically-valid-but-unsigned/garbage-signed transactions (easy and cheap to generate, requiring no stake or prior signature validity) forces every worker thread to pay full transaction parsing + cost-model computation cost for each one, with the payoff (dropping it) only happening afterward, and only if its computed priority happens to be at/below the floor. This is unpriced, unauthenticated CPU work injected ahead of the verification gate.

### Impact Explanation
This does not bypass signature verification or corrupt any recorded block; the impact is a CPU-amplification/QoS-evasion concern: the code adds a data-parsing- and cost-model-dependent computation stage — driven entirely by attacker-supplied, not-yet-authenticated bytes — in front of the fast, well-understood signature-check gate, and this extra stage only activates during congestion, i.e., exactly when sigverify-worker CPU headroom is scarcest. This is the class of "grossly underpriced pre-fee work" the validation rules call out.

### Likelihood Explanation
The path is reachable by any unprivileged remote peer able to reach the TPU/TPU-vote QUIC listener, with no stake or prior validated transaction required; the only precondition is that the scheduler has already published a non-zero priority floor (i.e., the node is under real transaction load), which is a normal operating condition for a highly-utilized validator, not an edge case.

### Recommendation
Consider reordering the pipeline so that (a) cheap syntactic/structural checks are used to gate the floor evaluation, or (b) `ed25519_verify_serial` runs before the priority-floor computation, so that unauthenticated/invalid-signature packets are dropped by the cheaper verification step before the parser and cost-model routines are invoked; alternatively, cap or rate-limit the per-worker CPU budget spent on priority-floor evaluation independent of the batch's authentication status.

### Proof of Concept
Not directly executable from the indexed code alone; the control flow shown above (`run_transaction_task` invoking `apply_priority_floor_to_batch` before `ed25519_verify_serial`, and `apply_priority_floor_to_batch` invoking full `SanitizedTransactionView`/`RuntimeTransaction` parsing plus `CostModel` computation on every un-deduped packet) demonstrates the ordering; a live PoC would require staking a sigverify worker under saturation and flooding it with syntactically valid, arbitrarily-signed transactions to measure CPU-time differential versus the pre-floor (non-saturated) baseline.

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

**File:** cost-model/src/cost_model.rs (L242-261)
```rust
    fn calculate_account_data_size_on_instruction(
        program_id: &Pubkey,
        instruction: SVMInstruction,
        feature_set: &FeatureSet,
    ) -> SystemProgramAccountAllocation {
        if program_id == &system_program::id() {
            if let Ok(instruction) =
                limited_deserialize(instruction.data, solana_packet::PACKET_DATA_SIZE as u64)
            {
                Self::calculate_account_data_size_on_deserialized_system_instruction(
                    instruction,
                    feature_set,
                )
            } else {
                SystemProgramAccountAllocation::Failed
            }
        } else {
            SystemProgramAccountAllocation::None
        }
    }
```

**File:** cost-model/src/cost_model.rs (L265-301)
```rust
    fn calculate_allocated_accounts_data_size<'a>(
        instructions: impl Iterator<Item = (&'a Pubkey, SVMInstruction<'a>)>,
        feature_set: &FeatureSet,
    ) -> u64 {
        let mut tx_attempted_allocation_size = Saturating(0u64);
        for (program_id, instruction) in instructions {
            match Self::calculate_account_data_size_on_instruction(
                program_id,
                instruction,
                feature_set,
            ) {
                SystemProgramAccountAllocation::Failed => {
                    // If any system program instructions can be statically
                    // determined to fail, no allocations will actually be
                    // persisted by the transaction. So return 0 here so that no
                    // account allocation budget is used for this failed
                    // transaction.
                    return 0;
                }
                SystemProgramAccountAllocation::None => continue,
                SystemProgramAccountAllocation::Some(ix_attempted_allocation_size) => {
                    tx_attempted_allocation_size += ix_attempted_allocation_size;
                }
            }
        }

        // The runtime prevents transactions from allocating too much account
        // data so clamp the attempted allocation size to the max amount.
        //
        // Note that if there are any custom bpf instructions in the transaction
        // it's tricky to know whether a newly allocated account will be freed
        // or not during an intermediate instruction in the transaction so we
        // shouldn't assume that a large sum of allocations will necessarily
        // lead to transaction failure.
        (MAX_PERMITTED_ACCOUNTS_DATA_ALLOCATIONS_PER_TRANSACTION as u64)
            .min(tx_attempted_allocation_size.0)
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
