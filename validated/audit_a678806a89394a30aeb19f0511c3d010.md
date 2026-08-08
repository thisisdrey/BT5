### Title
Cost model charges max declared `space` for `CreateAccount`-family instructions regardless of execution outcome, letting an unstaked attacker cheaply exhaust the per-slot `allocated_accounts_data_size` budget and block all account-creation transactions for the rest of the slot - ([File: cost-model/src/cost_model.rs])

### Summary
`CostModel::calculate_allocated_accounts_data_size` derives the account-data allocation charge purely from the *declared* `space` field of `System Program` instructions (`CreateAccount`, `CreateAccountWithSeed`, `Allocate`, `AllocateWithSeed`, `CreateAccountAllowPrefund`), without any dependency on whether the instruction actually succeeds at execution time. Because `MAX_PERMITTED_DATA_LENGTH` (~10MiB) is a large fraction of the whole block's `MAX_BLOCK_ACCOUNTS_DATA_SIZE_DELTA` (100MB), an unstaked attacker can submit roughly a dozen cheap, deliberately-failing `CreateAccount` transactions (e.g., with insufficient rent-exempt lamports) to fully exhaust the shared block-wide allocation budget for the remainder of the slot, rejecting all subsequent legitimate account-creating transactions with `WouldExceedAccountDataBlockLimit`.

### Finding Description
`CostModel::calculate_allocated_accounts_data_size` walks `program_instructions_iter()` and, for `system_program` instructions, statically deserializes them and reads `space` [1](#0-0) . The only "failure" case that zeroes out the charge is a *deserialization* failure or `space > MAX_PERMITTED_DATA_LENGTH`; a syntactically valid instruction that will fail at runtime (e.g., insufficient lamports for rent-exemption, account already in use) still returns `SystemProgramAccountAllocation::Some(space)`, contributing the full declared size [2](#0-1) .

This same computation is invoked from `calculate_cost_for_executed_transaction`, which is used post-execution by `Consumer::calculate_processed_transaction_costs` for every transaction whose `processing_result.processed_transaction()` is `Some` — this includes `Executed` (even with a failed status), `FeesOnly`, and `NoOp` variants, not only fully successful transactions [3](#0-2) [4](#0-3) . The resulting `TransactionCost` (with `allocated_accounts_data_size` set from declared, not actual, space) is then added into the shared `CostTracker` via `try_add_processed_transaction_costs -> cost_tracker.try_add` [5](#0-4) .

`CostTracker::would_fit` checks the running total `allocated_accounts_data_size` against `limits.allocated_data_size` (`MAX_BLOCK_ACCOUNTS_DATA_SIZE_DELTA` = 100,000,000 bytes) and rejects with `CostTrackerError::WouldExceedAccountDataBlockLimit` once exceeded [6](#0-5) ; on success it is unconditionally added via `add_transaction_cost` [7](#0-6) . There is no mechanism that reverts or discounts `allocated_accounts_data_size` for a transaction whose account-creation instruction actually failed at runtime — only whole-batch rollback paths (`remove_added_transaction_costs`, all-or-nothing rollback) remove it, and those only trigger on later-stage failures like PoH recording errors or block-cost/account-cost rejections, not on an individual System Program instruction failing inside an otherwise-processed transaction.

Since `MAX_PERMITTED_DATA_LENGTH` is a large fraction of `MAX_BLOCK_ACCOUNTS_DATA_SIZE_DELTA` (100,000,000 bytes total budget) [8](#0-7) , only a handful of transactions each declaring near-maximal `space` are needed to saturate the entire per-slot budget, regardless of whether the underlying `CreateAccount` actually succeeds (e.g., the attacker can set `lamports` too low to satisfy rent-exemption, causing the system program to fail cheaply and deterministically without ever allocating real account storage).

### Impact Explanation
Once the shared `allocated_accounts_data_size` counter is saturated, `CostTracker::would_fit` returns `Err(WouldExceedAccountDataBlockLimit)` for every subsequent transaction containing a `CreateAccount`-family instruction for the remainder of that leader's slot, regardless of the legitimate transaction's own account footprint. Because the tracker only resets when a fresh bank/slot is created (`new_from_parent_limits`), this is a per-slot QoS-evasion / block-wide DoS: an unstaked attacker can deny all account-creation transactions from being included for the whole slot at very low cost (a handful of minimum-fee transactions), which is a grossly underpriced pre-fee-work / QoS-evasion scenario matching the Agave cost-model/QoS bounty category.

### Likelihood Explanation
Preconditions are minimal: the attacker only needs enough lamports to pay a handful of standard transaction fees (no special privileges, no staking, no validator/leader control — an ordinary TPU client). The attack is deterministic and repeatable every slot the attacker targets, using publicly documented System Program instruction formats. No existing rate-limit, sigverify, or lock-based guard inspects the *plausibility* of the declared `space` versus actual account/lamport state before charging the cost-tracker budget.

### Recommendation
Do not charge `allocated_accounts_data_size` based purely on declared instruction data for transactions whose account-creation instruction did not actually succeed. Specifically, in `calculate_cost_for_executed_transaction` (or in `try_add_processed_transaction_costs`), gate the accounting of `allocated_accounts_data_size` on the transaction's actual execution `status`/`accounts_deltas` (already computed in `TransactionExecutionDetails::accounts_deltas`, as seen used in `runtime/src/bank.rs` for `accounts_data_len_delta`) rather than syntactic instruction inspection, or additionally revert/subtract the charge for `Executed` transactions whose relevant instruction failed. At minimum, cap the fraction of `MAX_BLOCK_ACCOUNTS_DATA_SIZE_DELTA` any single transaction/attacker can consume, or reconcile the pre-charged estimate against `accounts_resize_delta` after execution, symmetric to how `loaded_accounts_data_size` cost already uses actual post-execution values.

### Proof of Concept
Rust integration test plan (extend `core/src/banking_stage/consumer.rs` tests or `cost-model/src/cost_tracker.rs` tests):
1. Build ~10 `system_instruction::create_account` transactions, each with `space = MAX_PERMITTED_DATA_LENGTH` (or close to it) and `lamports = 0` (or below rent-exemption), so the System Program's runtime check fails deterministically (`InstructionError::InsufficientFundsForRent`-style) but the transaction is still `Executed`/`FeesOnly` (i.e., `processing_result.was_processed()` is `true`).
2. Feed them through `Consumer::process_and_record_transactions` against a bank with default `CostTrackerLimits` (`allocated_data_size = MAX_BLOCK_ACCOUNTS_DATA_SIZE_DELTA`).
3. Assert: (a) each transaction's status is `Err(..)` (rent/insufficient-funds failure) yet is committed/processed and charged a fee; (b) after ~10 transactions, `bank.read_cost_tracker().unwrap().would_fit(...)` (or a subsequent legitimate `create_account` transaction with a small, realistic `space`) returns `Err(CostTrackerError::WouldExceedAccountDataBlockLimit)`; (c) verify no real account data growth occurred (`accounts_resize_delta` ~ 0) despite the tracker being fully saturated, demonstrating the estimate-vs-actual mismatch.

### Citations

**File:** cost-model/src/cost_model.rs (L203-240)
```rust
    fn calculate_account_data_size_on_deserialized_system_instruction(
        instruction: SystemInstruction,
        feature_set: &FeatureSet,
    ) -> SystemProgramAccountAllocation {
        let validate_space = |space: u64| {
            if space > MAX_PERMITTED_DATA_LENGTH {
                SystemProgramAccountAllocation::Failed
            } else {
                SystemProgramAccountAllocation::Some(space)
            }
        };

        match instruction {
            SystemInstruction::CreateAccount { space, .. }
            | SystemInstruction::CreateAccountWithSeed { space, .. }
            | SystemInstruction::Allocate { space }
            | SystemInstruction::AllocateWithSeed { space, .. } => validate_space(space),
            SystemInstruction::CreateAccountAllowPrefund { space, .. } => {
                if !feature_set.snapshot().create_account_allow_prefund {
                    return SystemProgramAccountAllocation::Failed;
                }
                validate_space(space)
            }
            // DEVELOPER WARNING: New allocating instructions MUST return `Failed`
            // until activated by a feature gate
            SystemInstruction::Assign { .. }
            | SystemInstruction::Transfer { .. }
            | SystemInstruction::AdvanceNonceAccount
            | SystemInstruction::WithdrawNonceAccount(..)
            | SystemInstruction::InitializeNonceAccount(..)
            | SystemInstruction::AuthorizeNonceAccount(..)
            | SystemInstruction::UpgradeNonceAccount
            | SystemInstruction::AssignWithSeed { .. }
            | SystemInstruction::TransferWithSeed { .. } => SystemProgramAccountAllocation::None,
            // DEVELOPER WARNING: New non-allocating instructions MUST return `Failed`
            // until activated by a feature gate
        } // Do not add wildcard pattern (_)
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

**File:** core/src/banking_stage/consumer.rs (L490-520)
```rust
    fn calculate_processed_transaction_costs<'a, Tx: TransactionWithMeta>(
        bank: &Bank,
        transactions: &'a [Tx],
        processing_results: &[TransactionProcessingResult],
    ) -> Vec<Option<TransactionCost<'a, Tx>>> {
        let mut transaction_costs = Vec::with_capacity(processing_results.len());

        for (tx, processing_result) in transactions.iter().zip(processing_results) {
            let Some((executed_units, loaded_accounts_data_size)) = processing_result
                .processed_transaction()
                .map(|processed_tx| {
                    (
                        processed_tx.executed_units(),
                        processed_tx.loaded_accounts_data_size(),
                    )
                })
            else {
                transaction_costs.push(None);
                continue;
            };

            transaction_costs.push(Some(CostModel::calculate_cost_for_executed_transaction(
                tx,
                executed_units,
                loaded_accounts_data_size,
                &bank.feature_set,
            )));
        }

        transaction_costs
    }
```

**File:** core/src/banking_stage/consumer.rs (L542-563)
```rust
        let mut cost_tracker = bank.write_cost_tracker().unwrap();

        for (index, transaction_cost) in transaction_costs.iter_mut().enumerate() {
            let Some(cost) = transaction_cost.as_ref() else {
                continue;
            };

            match cost_tracker.try_add(cost) {
                Ok(_) => {}
                Err(err) => {
                    let transaction_error = TransactionError::from(err);
                    *transaction_cost = None;
                    if all_or_nothing {
                        all_or_nothing_error = Some((index, transaction_error));
                        break;
                    } else {
                        remaining_batch_error = Some((index, transaction_error));
                        break;
                    }
                }
            }
        }
```

**File:** svm/src/transaction_processing_result.rs (L19-29)
```rust
#[derive(Debug)]
pub enum ProcessedTransaction {
    /// Transaction was executed, but if execution failed, all account state changes
    /// will be rolled back except deducted fees and any advanced nonces
    Executed(Box<ExecutedTransaction>),
    /// Transaction was not able to be executed but fees are able to be
    /// collected and any nonces are advanceable
    FeesOnly(Box<FeesOnlyTransaction>),
    /// Transactions that cannot modify state but can still be processed
    NoOp(Box<NoOpTransaction>),
}
```

**File:** cost-model/src/cost_tracker.rs (L272-293)
```rust
    fn would_fit(
        &self,
        tx_cost: &TransactionCost<impl TransactionWithMeta>,
    ) -> Result<(), CostTrackerError> {
        let cost: u64 = tx_cost.sum();

        if self.block_cost().saturating_add(cost) > self.limits.block_cost {
            // check against the total package cost
            return Err(CostTrackerError::WouldExceedBlockMaxLimit);
        }

        // check if the transaction itself is more costly than the account_cost_limit
        if cost > self.limits.account_cost {
            return Err(CostTrackerError::WouldExceedAccountMaxLimit);
        }

        let allocated_accounts_data_size =
            self.allocated_accounts_data_size + Saturating(tx_cost.allocated_accounts_data_size());

        if allocated_accounts_data_size.0 > self.limits.allocated_data_size {
            return Err(CostTrackerError::WouldExceedAccountDataBlockLimit);
        }
```

**File:** cost-model/src/cost_tracker.rs (L312-323)
```rust
    // Returns the highest account cost for all write-lock accounts `TransactionCost` updated
    fn add_transaction_cost(&mut self, tx_cost: &TransactionCost<impl TransactionWithMeta>) -> u64 {
        self.allocated_accounts_data_size += tx_cost.allocated_accounts_data_size();
        self.transaction_count += 1;
        self.transaction_signature_count += tx_cost.num_transaction_signatures();
        self.secp256k1_instruction_signature_count +=
            tx_cost.num_secp256k1_instruction_signatures();
        self.ed25519_instruction_signature_count += tx_cost.num_ed25519_instruction_signatures();
        self.secp256r1_instruction_signature_count +=
            tx_cost.num_secp256r1_instruction_signatures();
        self.add_transaction_execution_cost(tx_cost, tx_cost.sum())
    }
```

**File:** cost-model/src/block_cost_limits.rs (L35-37)
```rust
/// The maximum allowed size, in bytes, that accounts data can grow, per block.
/// This can also be thought of as the maximum size of new allocations per block.
pub const MAX_BLOCK_ACCOUNTS_DATA_SIZE_DELTA: u64 = 100_000_000;
```
