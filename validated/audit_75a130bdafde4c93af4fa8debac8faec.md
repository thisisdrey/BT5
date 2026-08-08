### Title
Forwarding-stage priority heuristic admits unfunded, unpaid transactions at maximal priority, allowing fee-free eviction of legitimate transactions from the packet buffer - (File: `core/src/forwarding_stage.rs`)

### Summary
`calculate_priority` in `core/src/forwarding_stage.rs` computes `priority = MULTIPLIER * reward / (cost.sum() + 1)` purely from the *requested* fee fields in the raw transaction bytes (compute-budget instructions), never checking whether the fee payer actually has funds to cover that fee. Because `CostModel::estimate_cost`'s dominant cost terms (`SIGNATURE_COST`, `WRITE_LOCK_UNITS`) are fixed and small while `compute_unit_price` is an attacker-controlled `u64` with no floor tied to the compute-unit limit used for costing, an attacker can craft cheap, signature-valid, but unfunded transactions that receive near-maximal priority and evict genuinely payable transactions from the fixed-capacity `PacketContainer` before those transactions can be forwarded to the leader.

### Finding Description
`buffer_packet_batches` inserts every incoming packet into a fixed-capacity `PacketContainer` (`PacketContainer::with_capacity(4 * 4096)`), and once full, evicts the current minimum-priority entry via `pop_min` whenever a new packet has higher priority: [1](#0-0) 

Priority is computed by `calculate_priority`, which derives `reward` from `FeeDetails::new(signature_fee, prioritization_fee)` and `bank.calculate_reward_and_burn_fee_details(...)`, and derives `cost` from `CostModel::estimate_cost`: [2](#0-1) 

Critically, this computation only reads *fields declared in the transaction itself* (via `ComputeBudgetInstruction::set_compute_unit_price`/`set_compute_unit_limit`) — it never verifies that the fee payer account exists or holds sufficient lamports to actually pay `signature_fee + prioritization_fee`. That balance check only happens later, deep in bank transaction processing, not in the forwarding stage.

Meanwhile, `CostModel::estimate_cost`'s execution-cost component is simply the *requested* `compute_unit_limit` (not tied to `compute_unit_price`): [3](#0-2) , and the fixed-overhead terms (`SIGNATURE_COST`, `WRITE_LOCK_UNITS`) are small constants independent of price: [4](#0-3) 

An attacker can therefore set `compute_unit_limit` to a minimal value (keeping `cost.sum()` near its fixed floor of a few hundred/thousand units) while setting `compute_unit_price` to a very large value, making `priority_fee_lamports` (and thus `reward`) arbitrarily large: [5](#0-4) . The resulting `priority` — `MULTIPLIER * reward / (cost + 1)` — can be pushed far above any real, funded bidder's priority, since only the *promised* price matters, not the payer's actual ability to pay.

The packet only needs a cryptographically valid signature to reach `buffer_packet_batches` (this is unrelated to whether the signing keypair/account is funded) — an unprivileged attacker can generate an arbitrary number of fresh keypairs for free, sign a minimal transaction referencing a nonexistent/empty account as fee payer, and this transaction will pass sigverify and sanitization identically to a legitimate one, since neither performs a balance check.

### Impact Explanation
This is a QoS-evasion / grossly-underpriced-pre-fee-work issue in the `ForwardingStage` (`core/src/forwarding_stage.rs`, `PacketContainer` eviction). A flood of near-zero-cost, unfunded, price-inflated packets can dominate `min_priority` comparisons and repeatedly trigger `pop_min` evictions of genuinely payable, higher fee-paying transactions from the bounded 16384-capacity buffer, denying them forwarding to the upcoming leader — even though the attacker's own transactions will later fail fee-payer/balance checks in the bank and never actually pay anything. This degrades real users' transaction landing rate for zero cost to the attacker, matching the "QoS evasion / grossly underpriced pre-fee work" bounty category.

### Likelihood Explanation
Feasible for a fully unprivileged, unstaked remote client: it only requires generating disposable keypairs (free), constructing well-formed transactions with `ComputeBudgetInstruction::set_compute_unit_limit`/`set_compute_unit_price`, and sending them over the public TPU port. No stake, no funded accounts, no gossip/peer control is needed. It is repeatable at whatever rate the attacker can push valid-signature packets through ingest/sigverify, since each such packet is computationally cheap to produce (single-signature transfer or similarly minimal instruction set).

### Recommendation
Do not let uncollateralized, self-declared fee fields alone determine forwarding priority. Options include: bounding `compute_unit_price`'s contribution to `reward` by cross-checking (even loosely) the fee payer's known account balance from the root bank state before assigning priority; introducing a minimum plausibility check (e.g., reject/deprioritize packets whose fee payer account is unknown/underfunded relative to the declared fee) in `buffer_packet_batches`/`calculate_priority`; or rate-limiting/deprioritizing packets from fee payers that have never had a successfully landed transaction, so that unpaid "phantom" bids cannot cheaply displace real bids in the fixed-capacity `PacketContainer`.

### Proof of Concept
Rust integration test plan (to be run against `core::forwarding_stage`/`transaction_priority`):
1. Create a test bank with `lamports_per_signature > 0` (as in existing tests in `core/src/transaction_priority.rs`).
2. Construct transaction A: legitimate transfer from a funded keypair with a moderate `compute_unit_price` (e.g., 1_000) and default `compute_unit_limit`, sufficient to cover `signature_fee + prioritization_fee` from its own balance.
3. Construct transaction B: transfer/no-op from a freshly generated, **unfunded** keypair as fee payer, with `compute_unit_limit` set to the minimum allowed value and `compute_unit_price` set to `u64::MAX` (or a very large value), so `cost.sum()` stays near the fixed floor while `reward` is inflated.
4. Fill a `PacketContainer::with_capacity(N)` to capacity with transaction-A-like entries (simulating legitimate demand).
5. Feed transaction B through `buffer_packet_batches`/`calculate_priority` and assert:
   - `calculate_priority(B) > calculate_priority(A)` despite B being unfunded.
   - Inserting B causes `pop_min` to evict a valid transaction A from the container (`packet_container.is_full()` → `pop_min` path in `core/src/forwarding_stage.rs:315-331`).
   - When B would later be executed by the bank (fee-payer balance check), it fails/never lands, i.e., it consumed forwarding-buffer capacity and evicted a real bidder for zero actual payment.
6. Assert invariant violation: "no unfunded transaction should be able to evict a funded transaction from the forwarding buffer" fails under this construction.

### Citations

**File:** core/src/forwarding_stage.rs (L315-331)
```rust
            // If at capacity, check lowest priority item.
            if self.packet_container.is_full() {
                let min_priority = self.packet_container.min_priority().expect("not empty");
                // If priority of current packet is not higher than the min
                // drop the current packet.
                if min_priority >= priority {
                    self.metrics.votes_dropped_on_capacity += vote_count;
                    self.metrics.non_votes_dropped_on_capacity += non_vote_count;
                    continue;
                }

                let dropped_packet = self.packet_container.pop_min().expect("not empty");
                self.metrics.votes_dropped_on_capacity +=
                    usize::from(dropped_packet.meta().is_simple_vote_tx());
                self.metrics.non_votes_dropped_on_capacity +=
                    usize::from(!dropped_packet.meta().is_simple_vote_tx());
            }
```

**File:** core/src/forwarding_stage.rs (L601-640)
```rust
fn calculate_priority(
    transaction: &RuntimeTransaction<SanitizedTransactionView<&[u8]>>,
    bank: &Bank,
) -> Option<u64> {
    let transaction_configuration = transaction
        .transaction_configuration(&bank.feature_set)
        .ok()?;

    // Manually estimate fee here since currently interface doesn't allow a on SVM type.
    // Doesn't need to be 100% accurate so long as close and consistent.
    let prioritization_fee = transaction_configuration.priority_fee_lamports;
    let signature_details = transaction.signature_details();
    let signature_fee = signature_details
        .total_signatures()
        .saturating_mul(bank.fee_structure().lamports_per_signature);
    let fee_details = FeeDetails::new(signature_fee, prioritization_fee);

    let reward = bank
        .calculate_reward_and_burn_fee_details(&CollectorFeeDetails::from(fee_details))
        .get_deposit();

    let cost = CostModel::estimate_cost(
        transaction,
        transaction.program_instructions_iter(),
        transaction.num_requested_write_locks(),
        &bank.feature_set,
    );

    // We need a multiplier here to avoid rounding down too aggressively.
    // For many transactions, the cost will be greater than the fees in terms of raw lamports.
    // For the purposes of calculating prioritization, we multiply the fees by a large number so that
    // the cost is a small fraction.
    // An offset of 1 is used in the denominator to explicitly avoid division by zero.
    const MULTIPLIER: u64 = 1_000_000;
    Some(
        MULTIPLIER
            .saturating_mul(reward)
            .wrapping_div(cost.sum().saturating_add(1)),
    )
}
```

**File:** cost-model/src/cost_model.rs (L158-178)
```rust
    /// Return (programs_execution_cost, loaded_accounts_data_size_cost)
    fn get_estimated_execution_cost(
        transaction: &impl TransactionMeta,
        feature_set: &FeatureSet,
    ) -> (u64, u64) {
        // if failed to process compute_budget instructions, the transaction will not be executed
        // by `bank`, therefore it should be considered as no execution cost by cost model.
        let (programs_execution_costs, loaded_accounts_data_size_cost) =
            match transaction.transaction_configuration(feature_set) {
                Ok(config) => (
                    u64::from(config.compute_unit_limit),
                    Self::calculate_loaded_accounts_data_size_cost(
                        config.loaded_accounts_data_size_limit,
                        feature_set,
                    ),
                ),
                Err(_) => (0, 0),
            };

        (programs_execution_costs, loaded_accounts_data_size_cost)
    }
```

**File:** cost-model/src/block_cost_limits.rs (L9-18)
```rust
/// Number of compute units for one signature verification.
pub const SIGNATURE_COST: u64 = COMPUTE_UNIT_TO_US_RATIO * 24;
/// Number of compute units for one secp256k1 signature verification.
pub const SECP256K1_VERIFY_COST: u64 = COMPUTE_UNIT_TO_US_RATIO * 223;
/// Number of compute units for one ed25519 strict signature verification.
pub const ED25519_VERIFY_STRICT_COST: u64 = COMPUTE_UNIT_TO_US_RATIO * 80;
/// Number of compute units for one secp256r1 signature verification.
pub const SECP256R1_VERIFY_COST: u64 = COMPUTE_UNIT_TO_US_RATIO * 160;
/// Number of compute units for one write lock
pub const WRITE_LOCK_UNITS: u64 = COMPUTE_UNIT_TO_US_RATIO * 10;
```

**File:** runtime-transaction/src/transaction_meta.rs (L139-155)
```rust
    pub(crate) fn try_into_config(
        &self,
        feature_set: &FeatureSet,
    ) -> Result<TransactionConfiguration, TransactionError> {
        match self {
            Self::LegacyAndV0(compute_budget_instruction_details) => {
                let compute_budget_limits = compute_budget_instruction_details
                    .sanitize_and_convert_to_compute_budget_limits(feature_set)?;
                Ok(TransactionConfiguration {
                    updated_heap_bytes: compute_budget_limits.updated_heap_bytes,
                    compute_unit_limit: compute_budget_limits.compute_unit_limit,
                    priority_fee_lamports: compute_budget_limits.get_prioritization_fee(),
                    loaded_accounts_data_size_limit: compute_budget_limits
                        .loaded_accounts_bytes
                        .get(),
                })
            }
```
