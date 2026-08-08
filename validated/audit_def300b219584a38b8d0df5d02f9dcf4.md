#No Vulnerability found for this question.

The account-lock state machine in `ThreadAwareAccountLocks` is only ever mutated from the single scheduler thread that owns `SchedulingCommon` — `try_lock_accounts` (called from `try_schedule_transaction` in `GreedyScheduler::schedule`) and `complete_batch` (called from `SchedulingCommon::try_receive_completed`) execute sequentially in the same scheduler loop, not concurrently from multiple threads [1](#0-0) . There is no code path where a "second thread" independently mutates `self.account_locks` in parallel with the scheduler thread; worker threads only send `FinishedConsumeWork` messages back, which are drained via `try_recv` inside the same scheduler-owned call.

Additionally, `write_lock_account` explicitly enforces the safety invariant: if a write lock already exists, it asserts the new lock request is for the same `thread_id`, panicking otherwise, and `accounts_schedulable_threads` restricts the schedulable set to only the thread already holding a conflicting write lock [2](#0-1) [3](#0-2) . So `try_lock_accounts` can never grant a hot writable account to a second, different thread while the first thread's lock is outstanding — it will either restrict scheduling to the same thread (allowing safe re-scheduling on the already-executing thread, which is the documented intended design) or return `TryLockError::MultipleConflicts`/`ThreadNotAllowed` [4](#0-3) .

Locks are only released in `complete_batch`, which runs after `FinishedConsumeWork` is received, not before [5](#0-4) . The existing unit tests already assert these single-thread and panic invariants (e.g., `test_write_locking`, `test_try_lock_accounts_one`) [6](#0-5) [7](#0-6) .

The premise that "a lock to be granted to a second thread before `complete_batch` has unlocked the first thread's in-flight batch" is not supported by the code — there is no reachable path, race, or missing check that would allow this. This is architecturally prevented by (1) single-threaded ownership of `account_locks` by the scheduler, and (2) the explicit thread-matching assertions in `write_lock_account`/`read_lock_account`. No unprivileged-attacker-reachable exploit exists here.

### Citations

**File:** core/src/banking_stage/transaction_scheduler/scheduler_common.rs (L233-255)
```rust
impl<Tx: TransactionWithMeta> SchedulingCommon<Tx> {
    /// Receive completed batches of transactions.
    /// Returns `Ok((num_transactions, num_retryable))` if a batch was received, `Ok((0, 0))` if no batch was received.
    pub fn try_receive_completed(
        &mut self,
        container: &mut impl StateContainer<Tx>,
    ) -> Result<(usize, usize), SchedulerError> {
        match self.finished_consume_work_receiver.try_recv() {
            Ok(FinishedConsumeWork {
                work:
                    ConsumeWork {
                        batch_id,
                        mut ids,
                        mut transactions,
                        mut max_ages,
                    },
                retryable_indexes,
            }) => {
                let num_transactions = ids.len();
                let num_retryable = retryable_indexes.len();

                // Free the locks
                self.complete_batch(batch_id, &transactions);
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

**File:** scheduling-utils/src/thread_aware_account_locks.rs (L89-107)
```rust
    pub fn try_lock_accounts<'a>(
        &mut self,
        write_account_locks: impl Iterator<Item = &'a Pubkey> + Clone,
        read_account_locks: impl Iterator<Item = &'a Pubkey> + Clone,
        allowed_threads: ThreadSet,
        thread_selector: impl FnOnce(ThreadSet) -> ThreadId,
    ) -> Result<ThreadId, TryLockError> {
        let schedulable_threads = self
            .accounts_schedulable_threads(write_account_locks.clone(), read_account_locks.clone())
            .ok_or(TryLockError::MultipleConflicts)?;
        let schedulable_threads = schedulable_threads & allowed_threads;
        if schedulable_threads.is_empty() {
            return Err(TryLockError::ThreadNotAllowed);
        }

        let thread_id = thread_selector(schedulable_threads);
        self.lock_accounts(write_account_locks, read_account_locks, thread_id);
        Ok(thread_id)
    }
```

**File:** scheduling-utils/src/thread_aware_account_locks.rs (L167-203)
```rust
    fn schedulable_threads<const WRITE: bool>(&self, account: &Pubkey) -> ThreadSet {
        match self.locks.get(account) {
            None => ThreadSet::any(self.num_threads),
            Some(AccountLocks {
                write_locks: None,
                read_locks: Some(read_locks),
            }) => {
                if WRITE {
                    read_locks
                        .thread_set
                        .only_one_contained()
                        .map(ThreadSet::only)
                        .unwrap_or_else(ThreadSet::none)
                } else {
                    ThreadSet::any(self.num_threads)
                }
            }
            Some(AccountLocks {
                write_locks: Some(write_locks),
                read_locks: None,
            }) => ThreadSet::only(write_locks.thread_id),
            Some(AccountLocks {
                write_locks: Some(write_locks),
                read_locks: Some(read_locks),
            }) => {
                assert_eq!(
                    read_locks.thread_set.only_one_contained(),
                    Some(write_locks.thread_id)
                );
                read_locks.thread_set
            }
            Some(AccountLocks {
                write_locks: None,
                read_locks: None,
            }) => unreachable!(),
        }
    }
```

**File:** scheduling-utils/src/thread_aware_account_locks.rs (L225-255)
```rust
    /// Locks the given `account` for writing on `thread_id`.
    /// Panics if the account is already locked for writing on another thread.
    fn write_lock_account(&mut self, account: &Pubkey, thread_id: ThreadId) {
        let entry = self.locks.entry(*account).or_default();

        let AccountLocks {
            write_locks,
            read_locks,
        } = entry;

        if let Some(read_locks) = read_locks {
            assert_eq!(
                read_locks.thread_set.only_one_contained(),
                Some(thread_id),
                "outstanding read lock must be on same thread"
            );
        }

        if let Some(write_locks) = write_locks {
            assert_eq!(
                write_locks.thread_id, thread_id,
                "outstanding write lock must be on same thread"
            );
            write_locks.lock_count = write_locks.lock_count.wrapping_add(1);
        } else {
            *write_locks = Some(AccountWriteLocks {
                thread_id,
                lock_count: 1,
            });
        }
    }
```

**File:** scheduling-utils/src/thread_aware_account_locks.rs (L514-530)
```rust
    #[test]
    fn test_try_lock_accounts_one() {
        let pk1 = Pubkey::new_unique();
        let pk2 = Pubkey::new_unique();
        let mut locks = ThreadAwareAccountLocks::new(TEST_NUM_THREADS);
        locks.write_lock_account(&pk2, 3);

        assert_eq!(
            locks.try_lock_accounts(
                [&pk1].into_iter(),
                [&pk2].into_iter(),
                TEST_ANY_THREADS,
                test_thread_selector
            ),
            Ok(3)
        );
    }
```

**File:** scheduling-utils/src/thread_aware_account_locks.rs (L723-732)
```rust
    #[test]
    fn test_write_locking() {
        let pk1 = Pubkey::new_unique();
        let mut locks = ThreadAwareAccountLocks::new(TEST_NUM_THREADS);
        locks.write_lock_account(&pk1, 1);
        locks.write_lock_account(&pk1, 1);
        locks.write_unlock_account(&pk1, 1);
        locks.write_unlock_account(&pk1, 1);
        assert!(locks.locks.is_empty());
    }
```
