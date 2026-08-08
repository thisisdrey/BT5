### Title
Attacker-controlled batch of write/write-conflicting transactions extends account write-lock hold time, prolonging `AccountInUse` denial of a legitimate cross-batch transaction on the same account - ([File: accounts-db/src/account_locks.rs])

### Summary
`AccountLocks::try_lock_transaction_batch` validates every transaction's account keys against the *pre-batch* lock state before applying any locks, so multiple write-write conflicting transactions targeting the same writable account inside one batch all pass validation and all get their write lock counted. Because the whole batch's locks are released together only when the `TransactionBatch` (or the batch's `unlock_accounts` call) is dropped after the entire batch finishes executing, an attacker who packs many cheap, valid transactions that all declare the same writable account can keep that account's write lock held for the duration of the *whole batch*, not just a single transaction, denying a legitimate concurrently-submitted transaction on that account with `AccountInUse` for a proportionally longer window.

### Finding Description
`Accounts::lock_accounts` (`accounts-db/src/accounts.rs:455-474`) collects `(pubkey, is_writable)` iterators for every transaction in the batch via `TransactionAccountLocksIterator`, then calls `AccountLocks::try_lock_transaction_batch` under a single mutex acquisition [1](#0-0) .

Inside `try_lock_transaction_batch`, the implementation first loops over **all** transactions in the batch and calls `can_lock_accounts` — which only checks the pre-existing `write_locks`/`readonly_locks` maps, i.e., locks from *other, already-committed* batches — and only after this validation pass does it loop again to actually call `lock_accounts` (incrementing lock counters) for every transaction that passed [2](#0-1) . Because none of the sibling transactions' locks are applied during the check phase, two (or many) write-write-conflicting transactions referencing the same pubkey within one batch all pass `can_lock_accounts` and all get `lock_write` applied, incrementing the same counter multiple times. This is explicitly exercised and documented as intended by `test_accounts_locks_intrabatch_conflicts`, whose "ww conflict in-batch succeeds" assertion confirms multiple write locks on the same key inside one batch are both `Ok(())` [3](#0-2) , in contrast to the cross-batch case which always fails with `AccountInUse` [4](#0-3) .

The locks taken for the entire batch are released together: `TransactionBatch`'s `Drop` impl unlocks all transactions' accounts only once, after the whole batch has executed [5](#0-4) , and `Consumer::execute_and_commit_transactions_locked` executes the full batch (`bank.load_and_execute_transactions(batch, ...)`) before the batch's locks are ever released [6](#0-5) . Batches assembled by the leader's consume path are chunked up to `TARGET_NUM_TRANSACTIONS_PER_BATCH = 64` transactions [7](#0-6) , and `test_process_transactions_account_in_use` demonstrates that a batch of `TARGET_NUM_TRANSACTIONS_PER_BATCH` *distinct* transactions that all write the same destination account executes them **all sequentially inside one lock/unlock cycle**, with zero retries reported [8](#0-7) .

An unprivileged attacker can therefore submit ~64 valid, cheap transactions (e.g. system transfers, or transactions that simply list the target hot account as an unused writable account key — Solana does not require every listed account to be referenced by an instruction) that all write-lock the same target pubkey. They will be assembled into a single batch and processed together while holding a write lock on that pubkey for the entire batch's execution — not just one transaction's execution. Any legitimate, concurrently submitted transaction from another party that also writes that pubkey is bounced with `AccountInUse` (marked `immediately_retryable: true` per `execute_and_commit_transactions_locked`, `consumer.rs:250-257`) for the full duration of the attacker's batch instead of a single transaction's duration.

### Impact Explanation
This is a QoS/fairness-evasion issue: the account-lock mechanism's exclusivity invariant ("prevents conflicting transactions from executing concurrently") is technically preserved (no data race), but its secondary fairness property — bounding how long an outside transaction can be denied by a conflicting write lock — is violated. By constructing an in-batch chain of self-conflicting cheap writes, an attacker extends the exclusive-hold window on a targeted account roughly linearly with the number of transactions they can get bundled into one batch (up to the ~64-transaction chunking target), effectively multiplying the denial time a victim's competing transaction on that account experiences, at a cost proportional only to cheap transfer-fee-priced transactions rather than to genuinely contended, execution-heavy work.

### Likelihood Explanation
Fully reachable by an unstaked remote client: the attacker only needs to submit distinct, validly signed, low-cost transactions (different fee payers/nonces to avoid `AlreadyProcessed`/duplicate-hash rejection) that all reference one writable pubkey, timed to land in the same banking-stage batch. No special privileges, leader control, or gossip access are required — this is an ordinary TPU transaction submission pattern, and the relaxed in-batch ww-conflict behavior is a stable, tested code path (`test_accounts_locks_intrabatch_conflicts`), not a rare race.

### Recommendation
Bound the amount of extra exclusive-hold time a single batch can impose on one account by either: (1) capping how many conflicting write locks on the same pubkey may be taken within a single batch (rejecting/deferring the excess as `AccountInUse` even intra-batch), or (2) releasing/re-validating locks per-transaction as each transaction in the batch completes rather than holding all batch locks until the whole batch finishes, so a legitimate outside transaction can acquire the lock as soon as the specific conflicting transaction ahead of it finishes rather than waiting for the entire batch.

### Proof of Concept
Extend `accounts-db/src/accounts.rs`'s existing `test_accounts_locks_intrabatch_conflicts`-style test into a scaling benchmark based on `bench_entry_lock_accounts` (`accounts-db/benches/bench_lock_accounts.rs:66-102`):

```rust
#[test]
fn test_intrabatch_ww_conflict_extends_denial_window() {
    // Build increasing batch sizes of transactions that all write-lock the same pubkey.
    for &attacker_batch_size in &[1usize, 8, 32, 64] {
        let pubkey = Pubkey::new_unique();
        let accounts_db = Arc::new(AccountsDb::default_for_tests());
        let accounts = Accounts::new(accounts_db);

        let attacker_txs: Vec<_> = (0..attacker_batch_size)
            .map(|_| sanitized_tx_from_metas(vec![AccountMeta {
                pubkey, is_writable: true, is_signer: false,
            }]))
            .collect();

        // Lock the whole attacker batch at once (mirrors Consumer batch processing).
        let results = accounts.lock_accounts(
            attacker_txs.iter(),
            vec![Ok(()); attacker_batch_size].into_iter(),
            MAX_TX_ACCOUNT_LOCKS,
        );
        // All in-batch ww conflicts succeed regardless of batch size.
        assert!(results.iter().all(|r| r.is_ok()));

        // Victim's cross-batch transaction on the same account is denied
        // for as long as ANY attacker tx in the batch remains unlocked,
        // i.e. until the entire attacker batch is unlocked at once.
        let victim_tx = sanitized_tx_from_metas(vec![AccountMeta {
            pubkey, is_writable: true, is_signer: false,
        }]);
        let victim_result = accounts.lock_accounts(
            [victim_tx].iter(), [Ok(())].into_iter(), MAX_TX_ACCOUNT_LOCKS,
        );
        assert_eq!(victim_result, vec![Err(TransactionError::AccountInUse)]);

        // Unlocking a single attacker tx does NOT free the account while
        // others in the batch still hold it — demonstrating hold time scales
        // with attacker_batch_size, not with a single tx's execution time.
        accounts.unlock_accounts(std::iter::once(&attacker_txs[0]).zip(&results[..1]));
        if attacker_batch_size > 1 {
            let still_denied = accounts.lock_accounts(
                [victim_tx.clone()].iter(), [Ok(())].into_iter(), MAX_TX_ACCOUNT_LOCKS,
            );
            assert_eq!(still_denied, vec![Err(TransactionError::AccountInUse)]);
        }
    }
}
```
Expected result: the victim's lock attempt is denied by `AccountInUse` regardless of `attacker_batch_size`, and remains denied until *every* attacker transaction in the batch is unlocked — confirming the hold duration for the victim scales with the attacker's chosen batch size rather than being bounded to a single transaction's processing time, consistent with `TARGET_NUM_TRANSACTIONS_PER_BATCH` (`core/src/banking_stage/consumer.rs:37`) being the practical ceiling on this batch size.

### Citations

**File:** accounts-db/src/accounts.rs (L461-474)
```rust
        // Validate the account locks, then get keys and is_writable if successful validation.
        // We collect to fully evaluate before taking the account_locks mutex.
        let validated_batch_keys = txs
            .zip(results)
            .map(|(tx, result)| {
                result
                    .and_then(|_| validate_account_locks(tx.account_keys(), tx_account_lock_limit))
                    .map(|_| TransactionAccountLocksIterator::new(tx).accounts_with_is_writable())
            })
            .collect::<Vec<_>>();

        let account_locks = &mut self.account_locks.lock().unwrap();
        account_locks.try_lock_transaction_batch(validated_batch_keys)
    }
```

**File:** accounts-db/src/accounts.rs (L1259-1296)
```rust
        // wr conflict cross-batch always fails
        let results = accounts.lock_accounts(
            [r_tx.clone()].iter(),
            [Ok(())].into_iter(),
            MAX_TX_ACCOUNT_LOCKS,
        );

        assert_eq!(results, vec![Err(TransactionError::AccountInUse)]);

        // ww conflict cross-batch always fails
        let results = accounts.lock_accounts(
            [w_tx.clone()].iter(),
            [Ok(())].into_iter(),
            MAX_TX_ACCOUNT_LOCKS,
        );

        assert_eq!(results, vec![Err(TransactionError::AccountInUse)]);

        // wr conflict in-batch succeeds
        let accounts = Accounts::new(accounts_db.clone());
        let results = accounts.lock_accounts(
            [w_tx.clone(), r_tx.clone()].iter(),
            [Ok(()), Ok(())].into_iter(),
            MAX_TX_ACCOUNT_LOCKS,
        );

        assert_eq!(results, vec![Ok(()), Ok(())]);

        // ww conflict in-batch succeeds
        let accounts = Accounts::new(accounts_db);
        let results = accounts.lock_accounts(
            [w_tx, r_tx].iter(),
            [Ok(()), Ok(())].into_iter(),
            MAX_TX_ACCOUNT_LOCKS,
        );

        assert_eq!(results, vec![Ok(()), Ok(())]);
    }
```

**File:** accounts-db/src/account_locks.rs (L22-40)
```rust
    pub fn try_lock_transaction_batch<'a>(
        &mut self,
        mut validated_batch_keys: Vec<
            TransactionResult<impl Iterator<Item = (&'a Pubkey, bool)> + Clone>,
        >,
    ) -> Vec<TransactionResult<()>> {
        validated_batch_keys.iter_mut().for_each(|validated_keys| {
            if let Ok(keys) = validated_keys.as_ref()
                && let Err(e) = self.can_lock_accounts(keys.clone())
            {
                *validated_keys = Err(e);
            }
        });

        validated_batch_keys
            .into_iter()
            .map(|available_keys| available_keys.map(|keys| self.lock_accounts(keys)))
            .collect()
    }
```

**File:** runtime/src/transaction_batch.rs (L99-111)
```rust
// Unlock all locked accounts in destructor.
impl<Tx: SVMMessage> Drop for TransactionBatch<'_, '_, Tx> {
    fn drop(&mut self) {
        if self.needs_unlock() {
            self.set_needs_unlock(false);
            self.bank.unlock_accounts(
                self.sanitized_transactions()
                    .iter()
                    .zip(self.lock_results()),
            )
        }
    }
}
```

**File:** core/src/banking_stage/consumer.rs (L36-37)
```rust
/// Consumer will create chunks of transactions from buffer with up to this size.
pub const TARGET_NUM_TRANSACTIONS_PER_BATCH: usize = 64;
```

**File:** core/src/banking_stage/consumer.rs (L234-294)
```rust
    fn execute_and_commit_transactions_locked(
        &self,
        bank: &Bank,
        batch: &TransactionBatch<impl TransactionWithMeta>,
        flags: &ExecutionFlags,
    ) -> ExecuteAndCommitTransactionsOutput {
        let transaction_status_sender_enabled = self.committer.transaction_status_sender_enabled();
        let mut execute_and_commit_timings = LeaderExecuteAndCommitTimings::default();

        let mut error_counters = TransactionErrorMetrics::default();
        let mut retryable_transaction_indexes: Vec<_> = batch
            .lock_results()
            .iter()
            .enumerate()
            .filter_map(|(index, res)| match res {
                // Account lock conflicts are immediately retryable.
                Err(TransactionError::AccountInUse) => {
                    error_counters.account_in_use += 1;
                    // locking failure due to vote conflict or jito - immediately retry.
                    Some(RetryableIndex {
                        index,
                        immediately_retryable: true,
                    })
                }
                // following are non-retryable errors
                Err(TransactionError::TooManyAccountLocks) => {
                    error_counters.too_many_account_locks += 1;
                    None
                }
                Err(_) => None,
                Ok(_) => None,
            })
            .collect();

        let (load_and_execute_transactions_output, load_execute_us) =
            measure_us!(bank.load_and_execute_transactions(
                batch,
                bank.max_processing_age(),
                &mut execute_and_commit_timings.execute_timings,
                &mut error_counters,
                TransactionProcessingConfig {
                    account_overrides: None,
                    check_program_deployment_slot: bank.check_program_deployment_slot(),
                    log_messages_bytes_limit: self.log_messages_bytes_limit,
                    limit_to_load_programs: true,
                    recording_config: ExecutionRecordingConfig::new_single_setting(
                        transaction_status_sender_enabled
                    ),
                    drop_on_failure: flags.drop_on_failure,
                    all_or_nothing: flags.all_or_nothing,
                    strict_nonce_size_check: true,
                    drop_noop_transactions: true,
                }
            ));
        execute_and_commit_timings.load_execute_us = load_execute_us;

        let LoadAndExecuteTransactionsOutput {
            mut processing_results,
            mut processed_counts,
            balance_collector,
        } = load_and_execute_transactions_output;
```

**File:** core/src/banking_stage/consumer.rs (L1697-1769)
```rust
    #[test_case(false; "locked")]
    #[test_case(true; "duplicate")]
    fn test_process_transactions_account_in_use(use_duplicate_transaction: bool) {
        agave_logger::setup();
        let GenesisConfigInfo {
            genesis_config,
            mint_keypair,
            ..
        } = create_slow_genesis_config(10_000);
        let mut bank = Bank::new_for_tests(&genesis_config);
        bank.ns_per_slot = u128::MAX;
        let (bank, _bank_forks) = bank.wrap_with_bank_forks_for_tests();
        // set cost tracker limits to MAX so it will not filter out TXs
        bank.write_cost_tracker().unwrap().set_limits_max();

        let mut transactions = vec![];
        let destination = Pubkey::new_unique();
        let mut amount = 1;

        // Make distinct, or identical, transactions that conflict on the `mint_keypair`
        for _ in 0..TARGET_NUM_TRANSACTIONS_PER_BATCH {
            transactions.push(system_transaction::transfer(
                &mint_keypair,
                &destination,
                amount,
                genesis_config.hash(),
            ));

            if !use_duplicate_transaction {
                amount += 1;
            }
        }

        let transactions_len = transactions.len();
        let ProcessTransactionBatchOutput {
            execute_and_commit_transactions_output,
            ..
        } = execute_transactions_for_test(bank, transactions);

        // if the transactions are distinct, all are executed.
        // otherwise, only one is executed. regardless, all are attempted.
        let execution_count = if !use_duplicate_transaction {
            transactions_len
        } else {
            1
        } as u64;

        assert_eq!(
            execute_and_commit_transactions_output
                .transaction_counts
                .attempted_processing_count,
            transactions_len as u64
        );
        assert_eq!(
            execute_and_commit_transactions_output
                .transaction_counts
                .processed_count,
            execution_count
        );
        assert_eq!(
            execute_and_commit_transactions_output
                .transaction_counts
                .processed_with_successful_result_count,
            execution_count
        );

        // If the transactions are distinct, there are zero retryable (all executed).
        // If the transactions are identical, there are zero retryable (marked AlreadyProcessed).
        assert_eq!(
            execute_and_commit_transactions_output.retryable_transaction_indexes,
            Vec::<_>::new()
        );
    }
```
