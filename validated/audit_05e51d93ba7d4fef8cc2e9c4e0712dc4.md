Based on the code, this claim does not hold up. There is a clear separation of concerns between the two locking mechanisms, and the actual execution-time invariant is enforced uniformly across all code paths.

`ThreadAwareAccountLocks` in `scheduling-utils/src/thread_aware_account_locks.rs` is only a **scheduling-time filter** used by the central scheduler (`greedy_scheduler.rs`) to decide which worker thread a transaction should be routed to, allowing same-thread chaining so a worker isn't blocked waiting for currently in-flight transactions on the same thread to complete before scheduling more work to it.This confirms the invalidation. Both `consumer.process_and_record_transactions` (used by `vote_worker.rs`) and `consumer.process_and_record_aged_transactions` (used by the central-scheduler's `consume_worker.rs`/`ExternalWorker::execute_batch`) converge on the same `process_and_record_transactions_with_pre_results` function, which calls `bank.prepare_sanitized_batch_with_results` [1](#0-0) . That in turn calls `Bank::try_lock_accounts_with_results` → `Accounts::lock_accounts`, which takes the single shared `Mutex<AccountLocks>` on the bank's `Accounts` struct [2](#0-1) [3](#0-2) .

### Analysis

- `ThreadAwareAccountLocks` (in `scheduling-utils/src/thread_aware_account_locks.rs`) is purely a **pre-execution scheduling heuristic** used only inside the central-scheduler's `try_schedule_transaction` in `greedy_scheduler.rs` to pick which worker thread a transaction is dispatched to [4](#0-3) . It never gates real execution; it only decides routing, and unlocks happen when a batch completes via `complete_batch` in `scheduler_common.rs` [5](#0-4) .
- The **actual execution-time enforcement** is `Accounts::lock_accounts`, a real `Mutex<AccountLocks>` guard held for the duration of execution+commit and released only when the `TransactionBatch` is dropped [6](#0-5) . This mutex is shared globally on the `Bank`'s `Accounts` (`self.rc.accounts`), and is acquired unconditionally by **every** transaction-execution code path — both `vote_worker.rs`'s `consumer.process_and_record_transactions` and the central scheduler's `consumer.process_and_record_aged_transactions` (called from `consume_worker.rs`) — since both funnel into the same `process_and_record_transactions_with_pre_results` helper [7](#0-6) [8](#0-7) .
- Consequently, if a vote transaction and a non-vote transaction both write account X concurrently from different threads, whichever calls `bank.prepare_sanitized_batch_with_results` second will get `TransactionError::AccountInUse` from `Accounts::lock_accounts` and will not proceed to `execute_and_commit_transactions_locked` — it is deferred/retried, not executed concurrently. This is exactly the scenario exercised by `test_accounts_locks_multithreaded` and `test_bank_process_and_record_transactions_account_in_use`, which assert that `AccountInUse` blocks concurrent execution [9](#0-8) [10](#0-9) .

So the premise that "the scheduler's thread-aware lock check" is what gates concurrent execution is incorrect — `ThreadAwareAccountLocks` only affects intra-scheduler thread assignment, not cross-subsystem execution safety, which is always separately and correctly enforced by the bank-level `Accounts::lock_accounts` mutex regardless of which subsystem (vote worker, central scheduler, or external scheduler workers) is calling it.

#No vulnerability found for this question.

### Citations

**File:** core/src/banking_stage/consumer.rs (L199-216)
```rust
    fn process_and_record_transactions_with_pre_results(
        &self,
        bank: &Bank,
        txs: &[impl TransactionWithMeta],
        pre_results: impl Iterator<Item = Result<(), TransactionError>>,
        flags: &ExecutionFlags,
    ) -> ProcessTransactionBatchOutput {
        // Only lock accounts for transactions that passed pre-lock checks;
        // Once accounts are locked, other threads cannot encode transactions that will modify the
        // same account state
        let (batch, lock_us) =
            measure_us!(bank.prepare_sanitized_batch_with_results(txs, pre_results));

        let execute_and_commit_transactions_output =
            self.execute_and_commit_transactions_locked(bank, &batch, flags);

        // Once the accounts are new transactions can enter the pipeline to process them
        let (_, unlock_us) = measure_us!(drop(batch));
```

**File:** core/src/banking_stage/consumer.rs (L1565-1639)
```rust
    #[test_case(false; "locked")]
    #[test_case(true; "duplicate")]
    fn test_bank_process_and_record_transactions_account_in_use(use_duplicate_transaction: bool) {
        let TestFrame {
            mint_keypair,
            bank,
            bank_forks: _bank_forks,
            record_receiver: _record_receiver,
            consumer,
        } = setup_test(None);

        let pubkey = solana_pubkey::new_rand();
        let pubkey1 = solana_pubkey::new_rand();

        let transactions = sanitize_transactions(vec![
            system_transaction::transfer(&mint_keypair, &pubkey, 1, bank.last_blockhash()),
            system_transaction::transfer(
                &mint_keypair,
                if use_duplicate_transaction {
                    &pubkey
                } else {
                    &pubkey1
                },
                1,
                bank.last_blockhash(),
            ),
        ]);
        assert_eq!(
            transactions[0].message_hash() == transactions[1].message_hash(),
            use_duplicate_transaction
        );

        // with a duplicate transaction, we get a conflict from message hash equality in the batch
        // with no duplicate, we must take a cross-batch lock on an account to create a conflict
        if !use_duplicate_transaction {
            let conflicting_transaction =
                sanitize_transactions(vec![system_transaction::transfer(
                    &Keypair::new(),
                    &pubkey1,
                    1,
                    bank.last_blockhash(),
                )]);
            bank.try_lock_accounts(&conflicting_transaction);
        }

        let process_transactions_batch_output =
            consumer.process_and_record_transactions(&bank, &transactions);

        let ExecuteAndCommitTransactionsOutput {
            transaction_counts,
            retryable_transaction_indexes,
            commit_transactions_result,
            ..
        } = process_transactions_batch_output.execute_and_commit_transactions_output;

        assert_eq!(
            transaction_counts,
            LeaderProcessedTransactionCounts {
                attempted_processing_count: 2,
                processed_count: 1,
                processed_with_successful_result_count: 1,
            }
        );
        assert!(commit_transactions_result.is_ok());

        // duplicate transactions are not retryable
        if use_duplicate_transaction {
            assert_eq!(retryable_transaction_indexes, Vec::<_>::new());
        } else {
            assert_eq!(
                retryable_transaction_indexes,
                vec![RetryableIndex::new(1, true)]
            );
        }
    }
```

**File:** runtime/src/bank.rs (L3693-3721)
```rust
    /// Attempt to take locks on the accounts in a transaction batch, and their cost
    /// limited packing status and duplicate transaction conflict status
    pub fn try_lock_accounts_with_results(
        &self,
        txs: &[impl TransactionWithMeta],
        tx_results: impl Iterator<Item = Result<()>>,
    ) -> Vec<Result<()>> {
        let tx_account_lock_limit = self.get_transaction_account_lock_limit();

        // we must fail transactions that duplicate a prior message hash
        let mut batch_message_hashes = AHashSet::with_capacity(txs.len());
        let tx_results = tx_results
            .enumerate()
            .map(|(i, tx_result)| match tx_result {
                Ok(()) => {
                    // `HashSet::insert()` returns `true` when the value does *not* already exist
                    if batch_message_hashes.insert(txs[i].message_hash()) {
                        Ok(())
                    } else {
                        Err(TransactionError::AlreadyProcessed)
                    }
                }
                Err(e) => Err(e),
            });

        self.rc
            .accounts
            .lock_accounts(txs.iter(), tx_results, tx_account_lock_limit)
    }
```

**File:** accounts-db/src/accounts.rs (L452-474)
```rust
    /// This function will prevent multiple threads from modifying the same account state at the
    /// same time, possibly excluding transactions based on prior results
    #[must_use]
    pub fn lock_accounts<'a>(
        &self,
        txs: impl Iterator<Item = &'a (impl SVMMessage + 'a)>,
        results: impl Iterator<Item = Result<()>>,
        tx_account_lock_limit: usize,
    ) -> Vec<Result<()>> {
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

**File:** accounts-db/src/accounts.rs (L980-1062)
```rust
    #[test]
    fn test_accounts_locks_multithreaded() {
        let counter = Arc::new(AtomicU64::new(0));
        let exit = Arc::new(AtomicBool::new(false));

        let keypair0 = Keypair::new();
        let keypair1 = Keypair::new();
        let keypair2 = Keypair::new();

        let account0 = AccountSharedData::new(1, 0, &Pubkey::default());
        let account1 = AccountSharedData::new(2, 0, &Pubkey::default());
        let account2 = AccountSharedData::new(3, 0, &Pubkey::default());

        let accounts_db = AccountsDb::default_for_tests();
        let accounts = Accounts::new(Arc::new(accounts_db));
        accounts.store_for_tests(0, &keypair0.pubkey(), &account0);
        accounts.store_for_tests(0, &keypair1.pubkey(), &account1);
        accounts.store_for_tests(0, &keypair2.pubkey(), &account2);

        let accounts_arc = Arc::new(accounts);

        let instructions = vec![CompiledInstruction::new(2, &(), vec![0, 1])];
        let readonly_message = Message::new_with_compiled_instructions(
            1,
            0,
            2,
            vec![keypair0.pubkey(), keypair1.pubkey(), native_loader::id()],
            Hash::default(),
            instructions,
        );
        let readonly_tx = new_sanitized_tx(&[&keypair0], readonly_message, Hash::default());

        let instructions = vec![CompiledInstruction::new(2, &(), vec![0, 1])];
        let writable_message = Message::new_with_compiled_instructions(
            1,
            0,
            2,
            vec![keypair1.pubkey(), keypair2.pubkey(), native_loader::id()],
            Hash::default(),
            instructions,
        );
        let writable_tx = new_sanitized_tx(&[&keypair1], writable_message, Hash::default());

        let counter_clone = counter.clone();
        let accounts_clone = accounts_arc.clone();
        let exit_clone = exit.clone();
        thread::spawn(move || {
            loop {
                let txs = [writable_tx.clone()];
                let results = accounts_clone.clone().lock_accounts(
                    txs.iter(),
                    vec![Ok(()); txs.len()].into_iter(),
                    MAX_TX_ACCOUNT_LOCKS,
                );
                for result in results.iter() {
                    if result.is_ok() {
                        counter_clone.clone().fetch_add(1, Ordering::Release);
                    }
                }
                accounts_clone.unlock_accounts(txs.iter().zip(&results));
                if exit_clone.clone().load(Ordering::Relaxed) {
                    break;
                }
            }
        });
        let counter_clone = counter;
        for _ in 0..5 {
            let txs = [readonly_tx.clone()];
            let results = accounts_arc.clone().lock_accounts(
                txs.iter(),
                vec![Ok(()); txs.len()].into_iter(),
                MAX_TX_ACCOUNT_LOCKS,
            );
            if results[0].is_ok() {
                let counter_value = counter_clone.clone().load(Ordering::Acquire);
                thread::sleep(time::Duration::from_millis(50));
                assert_eq!(counter_value, counter_clone.clone().load(Ordering::Acquire));
            }
            accounts_arc.unlock_accounts(txs.iter().zip(&results));
            thread::sleep(time::Duration::from_millis(50));
        }
        exit.store(true, Ordering::Relaxed);
    }
```

**File:** core/src/banking_stage/transaction_scheduler/greedy_scheduler.rs (L245-276)
```rust
fn try_schedule_transaction<Tx: TransactionWithMeta>(
    transaction_state: &mut TransactionState<Tx>,
    account_locks: &mut ThreadAwareAccountLocks,
    schedulable_threads: ThreadSet,
    thread_selector: impl Fn(ThreadSet) -> ThreadId,
) -> Result<TransactionSchedulingInfo<Tx>, TransactionSchedulingError> {
    // Schedule the transaction if it can be.
    let transaction = transaction_state.transaction();
    let account_keys = transaction.account_keys();
    let write_account_locks = account_keys
        .iter()
        .enumerate()
        .filter_map(|(index, key)| transaction.is_writable(index).then_some(key));
    let read_account_locks = account_keys
        .iter()
        .enumerate()
        .filter_map(|(index, key)| (!transaction.is_writable(index)).then_some(key));

    let thread_id = match account_locks.try_lock_accounts(
        write_account_locks,
        read_account_locks,
        schedulable_threads,
        thread_selector,
    ) {
        Ok(thread_id) => thread_id,
        Err(TryLockError::MultipleConflicts) => {
            return Err(TransactionSchedulingError::UnschedulableConflicts);
        }
        Err(TryLockError::ThreadNotAllowed) => {
            return Err(TransactionSchedulingError::UnschedulableThread);
        }
    };
```

**File:** core/src/banking_stage/transaction_scheduler/scheduler_common.rs (L301-318)
```rust
    /// Mark a given `TransactionBatchId` as completed.
    /// This will update the internal tracking, including account locks.
    fn complete_batch(&mut self, batch_id: TransactionBatchId, transactions: &[Tx]) {
        let thread_id = self.in_flight_tracker.complete_batch(batch_id);
        for transaction in transactions {
            let account_keys = transaction.account_keys();
            let write_account_locks = account_keys
                .iter()
                .enumerate()
                .filter_map(|(index, key)| transaction.is_writable(index).then_some(key));
            let read_account_locks = account_keys
                .iter()
                .enumerate()
                .filter_map(|(index, key)| (!transaction.is_writable(index)).then_some(key));
            self.account_locks
                .unlock_accounts(write_account_locks, read_account_locks, thread_id);
        }
    }
```

**File:** accounts-db/src/account_locks.rs (L93-101)
```rust
    fn can_read_lock(&self, key: &Pubkey) -> bool {
        // If the key is not write-locked, it can be read-locked
        !self.is_locked_write(key)
    }

    fn can_write_lock(&self, key: &Pubkey) -> bool {
        // If the key is not read-locked or write-locked, it can be write-locked
        !self.is_locked_readonly(key) && !self.is_locked_write(key)
    }
```

**File:** core/src/banking_stage/vote_worker.rs (L343-354)
```rust
    /// Sends transactions to the bank.
    ///
    /// Returns the number of transactions successfully processed by the bank, which may be less
    /// than the total number if max PoH height was reached and the bank halted
    #[cfg_attr(test, qualifier_attr::qualifiers(pub(crate)))]
    fn process_transactions(
        consumer: &Consumer,
        bank: &Bank,
        transactions: &[impl TransactionWithMeta],
    ) -> ProcessTransactionsSummary {
        let process_transaction_batch_output =
            consumer.process_and_record_transactions(bank, transactions);
```

**File:** core/src/banking_stage/consume_worker.rs (L428-433)
```rust
            let output = self.consumer.process_and_record_aged_transactions(
                bank,
                &transactions,
                &max_ages,
                &execution_flags,
            );
```
