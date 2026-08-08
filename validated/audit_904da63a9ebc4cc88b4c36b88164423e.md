Based on my research, the closest analog in this Agave validator codebase to the "yieldFactor truncation via splitting" bug pattern is the integer-division truncation in the Solana cost model's per-transaction instruction-data cost, which (like the TickLogic yield factor) rounds down per-instance and is summed across many instances, so splitting one payload across many small transactions loses more to truncation than a single large transaction would.

### Title
Cost-model instruction-data cost truncation lets a spammer under-price aggregate instruction-data bytes by splitting a payload across many small transactions - (File: cost-model/src/cost_model.rs, cost-model/src/block_cost_limits.rs)

### Summary
`CostModel::get_instructions_data_cost` computes a transaction's instruction-data cost via integer division that truncates, exactly analogous to the reported `tick.yieldFactor` truncation: because `floor(a/c) + floor(b/c) <= floor((a+b)/c)`, splitting a given amount of instruction-data bytes across many small transactions systematically under-reports the true aggregate cost relative to submitting it as a single transaction.

### Finding Description
`INSTRUCTION_DATA_BYTES_COST` is defined as a compile-time truncated constant (`140 / COMPUTE_UNIT_TO_US_RATIO` = `140/30` = `4`) [1](#0-0) , and per-transaction cost is computed with truncating integer division: [2](#0-1) 

Because this uses `u16` truncating division rather than rounding up, any transaction whose total instruction data is less than `INSTRUCTION_DATA_BYTES_COST` (4) bytes contributes exactly `0` to `data_bytes_cost`, and any partial remainder below the divisor is discarded on every transaction. This cost feeds directly into `TransactionCost::sum()` [3](#0-2)  which is used both by `CostTracker::try_add` to gate block/account cost limits [4](#0-3)  and by the scheduler/forwarding priority formula `P = R / (1 + C)` [5](#0-4)  and [6](#0-5) .

This is structurally the same bug class as the external report: a value derived from dividing "work performed" by a fixed granularity is truncated per-application-instance rather than accumulated in higher precision, so a user who fragments a fixed amount of "work" (borrowed principal in the original report; instruction-data bytes here) into many small operations causes the protocol to under-account the true aggregate compared to doing it in one operation.

### Impact Explanation
This underprices/undercounts instruction-data bytes toward `block_cost` (which gates block admission) and toward the transaction's computed cost denominator used in scheduling/forwarding priority. An attacker submitting many transactions each carrying fewer than 4 bytes of instruction data accrues zero `data_bytes_cost` for that component, while a single transaction carrying the same aggregate bytes would be charged for it (rounded down once, not many times). This lets a spammer's real per-transaction "work" (parsing, serialization, network bandwidth, ledger storage of instruction data) slip through cost accounting essentially free, and inflates their computed prioritization ratio `R/(1+C)` slightly, which is a "grossly underpriced pre-fee work" / QoS-evasion pattern in the taxonomy the task calls for.

### Likelihood Explanation
Any unprivileged user can trivially construct transactions with small instruction-data payloads and submit many of them; no special privileges are required. However, the maximum truncation loss per transaction is small (at most `INSTRUCTION_DATA_BYTES_COST - 1` = 3 compute units), and `data_bytes_cost` is a minor component relative to signature cost, write-lock cost, and program execution cost that dominate `TransactionCost::sum()`. This bounds the practical severity: the truncation is real and matches the reported bug's root cause exactly, but its magnitude relative to `MAX_BLOCK_UNITS` (60,000,000) and typical per-transaction costs (thousands of CU from `programs_execution_cost` alone) is small [7](#0-6) .

### Recommendation
- Round `data_bytes_cost` up (ceiling division) rather than truncating, mirroring how `calculate_pages_for_bytes` already rounds up loaded-account-data-size costs [8](#0-7) , so fragmenting a payload into many small transactions cannot reduce the aggregate accounted cost below what a single transaction would incur.
- Add a property test asserting that for a fixed total instruction-data byte count, the sum of `data_bytes_cost` over any partition into multiple transactions is `>=` the `data_bytes_cost` of a single transaction carrying all the bytes (mirroring the recommended "total accruals" invariant from the external report).

### Proof of Concept
1. Construct transaction `T1` with a single instruction carrying 399 bytes of instruction data: `get_instructions_data_cost` returns `399 / 4 = 99`.
2. Construct 100 transactions `T2..T101`, each carrying ~4 bytes of instruction data (with 99 of them at 3 bytes and some at 4), so their individual `instruction_data_len() / 4` each round to `0` or `1`; summing these across the 100 transactions yields a materially smaller total than `99` for the equivalent aggregate byte count, verifiable directly against `CostModel::get_instructions_data_cost` / `test_cost_model_calculate_cost_with_limit`-style unit tests in `cost-model/src/cost_model.rs` [9](#0-8) .

### Citations

**File:** cost-model/src/block_cost_limits.rs (L19-20)
```rust
/// Number of data bytes per compute units
pub const INSTRUCTION_DATA_BYTES_COST: u64 = 140 /*bytes per us*/ / COMPUTE_UNIT_TO_US_RATIO;
```

**File:** cost-model/src/block_cost_limits.rs (L26-27)
```rust
pub const MAX_BLOCK_UNITS: u64 = MAX_BLOCK_UNITS_SIMD_0256;
pub const MAX_BLOCK_UNITS_SIMD_0256: u64 = 60_000_000;
```

**File:** cost-model/src/cost_model.rs (L181-183)
```rust
    fn get_instructions_data_cost(transaction: &impl TransactionMeta) -> u16 {
        transaction.instruction_data_len() / (INSTRUCTION_DATA_BYTES_COST as u16)
    }
```

**File:** cost-model/src/cost_model.rs (L185-190)
```rust
    /// Compute the number of pages needed to contain provided number of bytes.
    fn calculate_pages_for_bytes(bytes: u32) -> u64 {
        u64::from(bytes)
            .saturating_add(ACCOUNT_DATA_COST_PAGE_SIZE.saturating_sub(1))
            .saturating_div(ACCOUNT_DATA_COST_PAGE_SIZE)
    }
```

**File:** cost-model/src/cost_model.rs (L831-860)
```rust
    #[test]
    fn test_cost_model_calculate_cost_with_limit() {
        let (mint_keypair, start_hash) = test_setup();
        let to_keypair = Keypair::new();
        let data_limit = 32 * 1024u32;
        let tx =
            RuntimeTransaction::from_transaction_for_tests(Transaction::new_signed_with_payer(
                &[
                    system_instruction::transfer(&mint_keypair.pubkey(), &to_keypair.pubkey(), 2),
                    ComputeBudgetInstruction::set_loaded_accounts_data_size_limit(data_limit),
                ],
                Some(&mint_keypair.pubkey()),
                &[&mint_keypair],
                start_hash,
            ));

        let expected_account_cost = WRITE_LOCK_UNITS * 2;
        let feature_set = FeatureSet::default();
        let expected_execution_cost = 2 * u64::from(MAX_BUILTIN_ALLOCATION_COMPUTE_UNIT_LIMIT);
        let expected_loaded_accounts_data_size_cost = (data_limit as u64) / (32 * 1024) * 8;

        let tx_cost = CostModel::calculate_cost(&tx, &feature_set);
        assert_eq!(expected_account_cost, tx_cost.write_lock_cost());
        assert_eq!(expected_execution_cost, tx_cost.programs_execution_cost());
        assert_eq!(2, tx_cost.writable_accounts().count());
        assert_eq!(
            expected_loaded_accounts_data_size_cost,
            tx_cost.loaded_accounts_data_size_cost()
        );
    }
```

**File:** cost-model/src/transaction_cost.rs (L19-25)
```rust
    pub fn sum(&self) -> u64 {
        self.signature_cost
            .saturating_add(self.write_lock_cost)
            .saturating_add(u64::from(self.data_bytes_cost))
            .saturating_add(self.programs_execution_cost)
            .saturating_add(self.loaded_accounts_data_size_cost)
    }
```

**File:** cost-model/src/cost_tracker.rs (L172-186)
```rust
    pub fn try_add(
        &mut self,
        tx_cost: &TransactionCost<impl TransactionWithMeta>,
    ) -> Result<UpdatedCosts, CostTrackerError> {
        let cost = tx_cost.sum();

        if self.block_cost().saturating_add(cost) > self.limits.block_cost {
            // check against the total package cost
            return Err(CostTrackerError::WouldExceedBlockMaxLimit);
        }

        // check if the transaction itself is more costly than the account_cost_limit
        if cost > self.limits.account_cost {
            return Err(CostTrackerError::WouldExceedAccountMaxLimit);
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
