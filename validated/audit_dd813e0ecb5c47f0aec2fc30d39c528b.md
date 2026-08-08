### Title
Failed CreateAccount transactions permanently consume the block's account-data-allocation budget - ([File: cost-model/src/cost_model.rs])

### Summary
`CostModel::calculate_allocated_accounts_data_size` statically estimates a transaction's account-data allocation purely from its instructions, returning `0` only when the allocation can be *statically* proven invalid (bad deserialization, `space > MAX_PERMITTED_DATA_LENGTH`, or an inactive feature gate). It cannot detect a `CreateAccount`/`Allocate` that will fail at runtime due to insufficient rent-exempt balance in the target account. Because the exact same static estimator is reused for the *post-execution* "actual cost" calculation, transactions that are committed to the block (fee charged) but whose account-creation instruction fails at execution still get their full requested `space` charged against `CostTracker`'s block-wide `allocated_accounts_data_size` budget.

### Finding Description
Two cost computations exist for a transaction:
- Pre-execution estimate: `CostModel::calculate_cost` (`cost-model/src/cost_model.rs:36`).
- Post-execution "actual" cost: `CostModel::calculate_cost_for_executed_transaction` (`cost-model/src/cost_model.rs:56`), used by `get_transaction_costs` in `runtime/src/transaction_execution.rs:172-195` for any transaction whose commit result is `Ok` (i.e., it was committed/fee-charged), independent of whether the transaction's on-chain execution itself succeeded or errored.

Both paths compute `allocated_accounts_data_size` via the private helper `CostModel::calculate_allocated_accounts_data_size` (`cost-model/src/cost_model.rs:265-301`), which only zeroes out the allocation when the instruction can be *statically* determined to fail (see `SystemProgramAccountAllocation::Failed` handling at lines 276-283, fed by `calculate_account_data_size_on_deserialized_system_instruction` at lines 203-240). A `CreateAccount`/`Allocate`/`AllocateWithSeed` instruction with a valid, in-range `space` (≤ `MAX_PERMITTED_DATA_LENGTH`) is always treated as `SystemProgramAccountAllocation::Some(space)`, even if the *target account* will not end up rent-exempt and the instruction will actually abort during execution (`TransactionError::InsufficientFundsForRent`, confirmed by `runtime/src/bank/tests.rs:10232` `test_invalid_rent_state_changes_new_accounts` and `runtime/src/bank/tests.rs:10906`).

Crucially, as long as the **fee payer** (which can be different from the account being created) has enough lamports to cover the transaction fee, the transaction passes fee-payer/loading validation, is executed, fails only on the `CreateAccount` instruction, and is still **committed** (fee charged, included in the block) with an error status. `get_transaction_costs` (`runtime/src/transaction_execution.rs:182-193`) treats any `Ok(committed_tx)` commit result as "processed" and recomputes cost from the *same static instruction-based estimator*, not from the actual (zero) `accounts_resize_delta` recorded in `TransactionExecutionDetails::accounts_deltas` (see `runtime/src/bank.rs:4419-4430`, which correctly nets `accounts_resize_delta` to 0 for failed transactions at the bank-level `accounts_data_size` counter — but this correction is never propagated back into `CostTracker`).

`check_block_cost_limits` (`runtime/src/transaction_execution.rs:157-169`) then calls `CostTracker::try_add` → `would_fit` (`cost-model/src/cost_tracker.rs:272-293`) and `add_transaction_cost` (`cost-model/src/cost_tracker.rs:313-323`), which unconditionally adds `tx_cost.allocated_accounts_data_size()` to `self.allocated_accounts_data_size`, checked against `CostTrackerLimits::allocated_data_size` (default `MAX_BLOCK_ACCOUNTS_DATA_SIZE_DELTA = 100_000_000`, `cost-model/src/block_cost_limits.rs:37`). There is no removal/rollback of this component when the underlying account-creation instruction actually failed at runtime.

### Impact Explanation
An unprivileged remote sender can submit ordinary, signed transactions (`CreateAccount` targeting `space` close to `MAX_PERMITTED_DATA_LENGTH` ≈ 10 MiB, funding the new account with 0 or insufficient lamports) where only the fee payer needs enough SOL to cover the base transaction fee. Roughly 10 such transactions (10 MiB × 10 ≈ 100 MB) exhaust the entire per-slot `MAX_BLOCK_ACCOUNTS_DATA_SIZE_DELTA` budget tracked in `CostTracker::allocated_accounts_data_size`, causing `would_fit` to reject every subsequent legitimate `CreateAccount`/`Allocate` transaction in that slot with `CostTrackerError::WouldExceedAccountDataBlockLimit`, even though zero bytes of account data were actually persisted. This is a QoS-evasion / grossly underpriced pre-fee-relative-to-consumed-scarce-resource issue: the attacker pays only normal per-signature transaction fees while consuming the maximum possible share of a scarce, cluster-wide, per-slot resource.

### Likelihood Explanation
Fully reachable by an unstaked remote client with only the ability to fund a fee payer with enough lamports for a handful of base transaction fees plus a throwaway keypair per attempted `CreateAccount`. No special privileges, gossip/staking, or leader control needed — the attacker simply sends normal transactions to the current leader's TPU. The condition (fee payer funded, target account not rent exempt) is trivial to construct deterministically and repeatably every slot the attacker gets to submit transactions to a leader.

### Recommendation
When computing the post-execution "actual" cost in `get_transaction_costs`/`CostModel::calculate_cost_for_executed_transaction`, base `allocated_accounts_data_size` on the transaction's actual recorded `accounts_resize_delta` (already available via `TransactionExecutionDetails::accounts_deltas`, see `runtime/src/bank.rs:4419-4430`) instead of re-deriving it from the static per-instruction estimate. If the transaction execution status is an error, the actual data-size growth is provably 0 and the cost tracker should be credited/charged 0 for `allocated_accounts_data_size`, consistent with how `programs_execution_cost` and `loaded_accounts_data_size_cost` are already replaced with real, measured values in the same function.

### Proof of Concept
```rust
// cost-model/src/cost_tracker.rs (extend existing test module)
#[test]
fn test_failed_create_account_still_consumes_allocated_data_budget() {
    use solana_system_interface::MAX_PERMITTED_DATA_LENGTH;

    let mint_keypair = test_setup();
    let payer = Keypair::new(); // funded only enough to pay the tx fee
    let new_account = Keypair::new(); // funded with 0 lamports -> not rent exempt

    // Build a CreateAccount tx requesting MAX_PERMITTED_DATA_LENGTH space
    // with 0 lamports transferred to new_account (fails InsufficientFundsForRent
    // at execution, but the fee payer has enough balance for the tx fee).
    let tx = build_create_account_tx(&payer, &new_account, 0, MAX_PERMITTED_DATA_LENGTH);
    let tx_cost = simple_transaction_cost(&tx, 5); // pre-execution style estimate
    // Simulate the post-execution "actual" cost calculation: since the
    // instruction is *statically* valid, allocated_accounts_data_size is
    // still MAX_PERMITTED_DATA_LENGTH even though the account creation failed.
    let mut executed_tx_cost = tx_cost.clone();
    executed_tx_cost.allocated_accounts_data_size = MAX_PERMITTED_DATA_LENGTH; // as produced by calculate_cost_for_executed_transaction

    let mut tracker = CostTracker::default();
    // 10 such "failed" transactions exhaust the 100_000_000-byte budget.
    for _ in 0..10 {
        assert!(tracker.try_add(&executed_tx_cost).is_ok());
    }

    // A legitimate, successful CreateAccount transaction now gets rejected,
    // even though no attacker transaction actually grew accounts_data_size.
    let legit_tx = build_create_account_tx(&Keypair::new(), &Keypair::new(), 1_000_000, 100);
    let legit_cost = simple_transaction_cost(&legit_tx, 5);
    assert_eq!(
        tracker.would_fit(&legit_cost),
        Err(CostTrackerError::WouldExceedAccountDataBlockLimit)
    );
}
```
Complementary integration-level assertion (in `runtime/src/transaction_execution.rs` tests or `core/src/banking_stage/consumer.rs` tests): submit N `CreateAccount` transactions with underfunded target accounts through `execute_batch`/`process_and_record_transactions`, assert each commits with `TransactionError::InsufficientFundsForRent`, then assert `bank.read_cost_tracker().unwrap()` shows `allocated_accounts_data_size` incremented by the full requested `space` for each, and that `bank.load_accounts_data_size_delta()` (the real, global counter) remains unchanged at 0 — demonstrating the divergence between the real accounts-data growth (0) and the cost-tracker's charged budget (non-zero, up to `MAX_BLOCK_ACCOUNTS_DATA_SIZE_DELTA`). [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** cost-model/src/cost_model.rs (L56-77)
```rust
    pub fn calculate_cost_for_executed_transaction<'a, Tx: TransactionMeta + SVMStaticMessage>(
        transaction: &'a Tx,
        actual_programs_execution_cost: u64,
        actual_loaded_accounts_data_size_bytes: u32,
        feature_set: &FeatureSet,
    ) -> TransactionCost<'a, Tx> {
        let loaded_accounts_data_size_cost = Self::calculate_loaded_accounts_data_size_cost(
            actual_loaded_accounts_data_size_bytes,
            feature_set,
        );
        let instructions_data_cost = Self::get_instructions_data_cost(transaction);

        Self::calculate_transaction_cost(
            transaction,
            transaction.program_instructions_iter(),
            transaction.num_write_locks(),
            actual_programs_execution_cost,
            loaded_accounts_data_size_cost,
            instructions_data_cost,
            feature_set,
        )
    }
```

**File:** cost-model/src/cost_model.rs (L263-301)
```rust
    /// eventually, potentially determine account data size of all writable accounts
    /// at the moment, calculate account data size of account creation
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

**File:** runtime/src/transaction_execution.rs (L157-195)
```rust
fn check_block_cost_limits<Tx: TransactionWithMeta>(
    bank: &Bank,
    tx_costs: &[Option<TransactionCost<'_, Tx>>],
) -> TransactionResult<()> {
    let mut cost_tracker = bank.write_cost_tracker().unwrap();
    for tx_cost in tx_costs.iter().flatten() {
        cost_tracker
            .try_add(tx_cost)
            .map_err(TransactionError::from)?;
    }

    Ok(())
}

// Get actual transaction execution costs from transaction commit results
fn get_transaction_costs<'a, Tx: TransactionWithMeta>(
    bank: &Bank,
    commit_results: &[TransactionCommitResult],
    sanitized_transactions: &'a [Tx],
) -> Vec<Option<TransactionCost<'a, Tx>>> {
    assert_eq!(sanitized_transactions.len(), commit_results.len());

    commit_results
        .iter()
        .zip(sanitized_transactions)
        .map(|(commit_result, tx)| {
            if let Ok(committed_tx) = commit_result {
                Some(CostModel::calculate_cost_for_executed_transaction(
                    tx,
                    committed_tx.executed_units,
                    committed_tx.loaded_account_stats.loaded_accounts_data_size,
                    &bank.feature_set,
                ))
            } else {
                None
            }
        })
        .collect()
}
```

**File:** cost-model/src/cost_tracker.rs (L272-323)
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

        // check each account against account_cost_limit,
        for account_key in tx_cost.writable_accounts() {
            match self.cost_by_writable_accounts.get(account_key) {
                Some(chained_cost) => {
                    if chained_cost.saturating_add(cost) > self.limits.account_cost {
                        return Err(CostTrackerError::WouldExceedAccountMaxLimit);
                    } else {
                        continue;
                    }
                }
                None => continue,
            }
        }

        Ok(())
    }

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

**File:** runtime/src/bank.rs (L4419-4430)
```rust
        let accounts_data_len_delta = processing_results
            .iter()
            .filter_map(|processing_result| processing_result.processed_transaction())
            .filter_map(|processed_tx| processed_tx.execution_details())
            .filter_map(|details| details.accounts_deltas.as_ref())
            .map(|deltas| {
                deltas
                    .accounts_resize_delta
                    .saturating_sub_unsigned(deltas.accounts_uninitialized_size)
            })
            .sum();
        self.update_accounts_data_size_delta_on_chain(accounts_data_len_delta);
```

**File:** runtime/src/bank/tests.rs (L10232-10284)
```rust
#[test]
fn test_invalid_rent_state_changes_new_accounts() {
    let GenesisConfigInfo {
        mut genesis_config,
        mint_keypair,
        ..
    } = create_genesis_config_with_leader(100 * LAMPORTS_PER_SOL, &Pubkey::new_unique(), 42);
    genesis_config.rent = Rent::default();

    let mock_program_id = Pubkey::new_unique();
    let account_data_size = 100;
    let rent_exempt_minimum = genesis_config.rent.minimum_balance(account_data_size);

    let (bank, _bank_forks) = Bank::new_with_mockup_builtin_for_tests(
        &genesis_config,
        mock_program_id,
        MockTransferBuiltin::register,
    );
    let recent_blockhash = bank.last_blockhash();

    let check_account_is_rent_exempt = |pubkey: &Pubkey| -> bool {
        let account = bank.get_account(pubkey).unwrap();
        Rent::default().is_exempt(account.lamports(), account.data().len())
    };

    // Try to create RentPaying account
    let rent_paying_account = Keypair::new();
    let tx = system_transaction::create_account(
        &mint_keypair,
        &rent_paying_account,
        recent_blockhash,
        rent_exempt_minimum - 1,
        account_data_size as u64,
        &mock_program_id,
    );
    let result = bank.process_transaction(&tx);
    assert!(result.is_err());
    assert!(bank.get_account(&rent_paying_account.pubkey()).is_none());

    // Try to create RentExempt account
    let rent_exempt_account = Keypair::new();
    let tx = system_transaction::create_account(
        &mint_keypair,
        &rent_exempt_account,
        recent_blockhash,
        rent_exempt_minimum,
        account_data_size as u64,
        &mock_program_id,
    );
    let result = bank.process_transaction(&tx);
    assert!(result.is_ok());
    assert!(check_account_is_rent_exempt(&rent_exempt_account.pubkey()));
}
```
