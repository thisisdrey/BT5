### Title
Unbounded write starvation via multi-thread read-lock cycling on a shared account - (File: scheduling-utils/src/thread_aware_account_locks.rs)

### Summary
`ThreadAwareAccountLocks::schedulable_threads::<true>` (used by `write_schedulable_threads`) only permits scheduling a write transaction when the account's outstanding read locks are held on exactly one thread (`only_one_contained()`), returning `ThreadSet::none()` otherwise. Because `greedy_scheduler::schedule` processes transactions in strict priority order and requeues failed writers into `unschedulables` for the next pass, an attacker who continuously supplies fresh, cheap read-only transactions on a popular account — scheduled across ≥2 worker threads via `select_thread`'s load-balancing — can keep `AccountReadLocks::thread_set` spanning multiple threads indefinitely, permanently blocking any write transaction (including a victim's fee-paying transfer) that touches the same account.

### Finding Description
`schedulable_threads::<true>` in `scheduling-utils/src/thread_aware_account_locks.rs` computes write-schedulability as: [1](#0-0) 

When only read locks exist and they span more than one thread, `only_one_contained()` returns `None`, so `write_schedulable_threads` returns `ThreadSet::none()`, and `accounts_schedulable_threads` in `try_lock_accounts` then yields `TryLockError::MultipleConflicts` for any write attempt: [2](#0-1) 

In `GreedyScheduler::schedule`, transactions are popped from the container in strict priority order (`container.pop()`), and a transaction that fails to lock (`UnschedulableConflicts`/`UnschedulableThread`) is pushed to `self.unschedulables` and requeued at the end of the pass via `container.push_ids_into_queue`, but scanning continues to lower-priority transactions in the same pass: [3](#0-2) [4](#0-3) 

Because lower-priority read-only transactions never conflict with each other, they continue to be freely scheduled (`read_schedulable_threads` always returns `ThreadSet::any`) after the higher-priority write fails, and each such read can be assigned to a different thread by `select_thread`'s load-balancing thread selector, keeping `AccountReadLocks::thread_set` spread across ≥2 threads: [5](#0-4) [6](#0-5) 

An attacker only needs the capability described in the question: an unbounded stream of valid, low-fee, read-only transactions referencing the target/victim account, interleaved across scheduling passes fast enough that at least two distinct threads always hold outstanding read locks on that account. Nothing in `try_lock_accounts`, `schedulable_threads`, or `GreedyScheduler::schedule` bounds how many consecutive passes a write transaction can be pushed into `unschedulables`; there is no reader-yields-to-writer, write-priority-boost, or thread-affinity-pinning mechanism to force read locks to converge to one thread. The only implicit bound (`max_scanned_transactions_per_scheduling_pass`) limits scanning within a single pass, not the number of passes across which a specific write transaction can starve.

### Impact Explanation
This is an account-lock correctness/fairness violation: an unstaked attacker who can only submit valid signed transactions (no leader/validator/staked control) can indefinitely delay or effectively strand a victim's legitimate, fee-paying write transaction to any account the attacker chooses to spam with read-only references. Given transaction TTL (`max_age` / blockhash expiration), a sufficiently long starvation window causes the victim's transaction to be dropped/expired even though the victim paid competitive fees — a QoS evasion / DoS on ordinary users' transactions, matching the stated bounty scope of "permanently strand or silently drop other users' fee-paying transactions through lock conflicts."

### Likelihood Explanation
Feasible with only unprivileged capabilities: the attacker needs a supply of funded fee-payer keys to keep submitting cheap read-only transactions against the target account, continuously refreshing outstanding read locks before they collapse to a single thread. No special account balances, staking, or validator control are required. The reads do not need to outbid the victim's priority fee — they only need to remain below it in the queue so the victim's write is scanned first, fails, and is requeued, while the reads behind it keep succeeding and diversifying `thread_set`. This is repeatable every scheduling pass with no built-in termination bound.

### Recommendation
Introduce an anti-starvation mechanism in `ThreadAwareAccountLocks`/`GreedyScheduler`, e.g.: (1) track consecutive failed-scheduling attempts per transaction/account and, once a threshold is exceeded, temporarily restrict new read-lock grants on that account to the single thread already holding the plurality of locks (forcing convergence), or (2) give a write transaction that has been requeued N times priority to reserve a thread once any read locks on its account drain to a single thread, blocking further cross-thread read scheduling on that account until the pending write is granted.

### Proof of Concept
Unit/invariant test targeting `scheduling-utils/src/thread_aware_account_locks.rs` (add to its `#[cfg(test)] mod tests`):

```rust
#[test]
fn test_write_starvation_via_cycling_reads() {
    let pk = Pubkey::new_unique();
    let mut locks = ThreadAwareAccountLocks::new(TEST_NUM_THREADS);

    // Simulate an unbounded stream of attacker read transactions that keep
    // 2 distinct threads holding read locks on `pk`, never collapsing to one.
    for round in 0..10_000 {
        let t0 = round % 2;
        let t1 = (round + 1) % 2;
        locks.read_lock_account(&pk, t0);
        locks.read_lock_account(&pk, t1);

        // Victim's write transaction attempt every round: must never succeed
        // while >1 thread holds read locks.
        assert_eq!(
            locks.accounts_schedulable_threads([&pk].into_iter(), std::iter::empty()),
            None,
            "write must be starved while reads span >1 thread (round {round})"
        );

        // Attacker "finishes" the oldest read on one thread and issues a new
        // one on the other thread, keeping thread_set width == 2 forever.
        locks.read_unlock_account(&pk, t0);
        locks.read_lock_account(&pk, t0);
    }
    // After 10,000 simulated scheduling passes, the write is still unschedulable.
    assert_eq!(
        locks.accounts_schedulable_threads([&pk].into_iter(), std::iter::empty()),
        None
    );
}
```

Expected assertion: the write transaction's schedulable thread set remains `None`/empty across an arbitrarily large number of rounds, demonstrating unbounded starvation. A stronger property/fuzz test should further drive this through `GreedyScheduler::schedule` with a `TransactionStateContainer` seeded with a high-priority write and a continuous stream of lower-priority reads across 2+ `consume_work_senders`, asserting that `SchedulingSummary::num_scheduled` for the write transaction remains 0 after a bounded number of `schedule()` calls proportional to container size (currently it is not bounded at all).

### Citations

**File:** scheduling-utils/src/thread_aware_account_locks.rs (L126-148)
```rust
    fn accounts_schedulable_threads<'a>(
        &self,
        write_account_locks: impl Iterator<Item = &'a Pubkey>,
        read_account_locks: impl Iterator<Item = &'a Pubkey>,
    ) -> Option<ThreadSet> {
        let mut schedulable_threads = ThreadSet::any(self.num_threads);

        for account in write_account_locks {
            schedulable_threads &= self.write_schedulable_threads(account);
            if schedulable_threads.is_empty() {
                return None;
            }
        }

        for account in read_account_locks {
            schedulable_threads &= self.read_schedulable_threads(account);
            if schedulable_threads.is_empty() {
                return None;
            }
        }

        Some(schedulable_threads)
    }
```

**File:** scheduling-utils/src/thread_aware_account_locks.rs (L150-158)
```rust
    /// Returns `ThreadSet` of schedulable threads for the given readable account.
    fn read_schedulable_threads(&self, account: &Pubkey) -> ThreadSet {
        self.schedulable_threads::<false>(account)
    }

    /// Returns `ThreadSet` of schedulable threads for the given writable account.
    fn write_schedulable_threads(&self, account: &Pubkey) -> ThreadSet {
        self.schedulable_threads::<true>(account)
    }
```

**File:** scheduling-utils/src/thread_aware_account_locks.rs (L167-183)
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
```

**File:** scheduling-utils/src/thread_aware_account_locks.rs (L302-317)
```rust
        match read_locks {
            Some(read_locks) => {
                read_locks.thread_set.insert(thread_id);
                read_locks.lock_counts[thread_id] =
                    read_locks.lock_counts[thread_id].wrapping_add(1);
            }
            None => {
                let mut lock_counts = [0; MAX_THREADS];
                lock_counts[thread_id] = 1;
                *read_locks = Some(AccountReadLocks {
                    thread_set: ThreadSet::only(thread_id),
                    lock_counts,
                });
            }
        }
    }
```

**File:** core/src/banking_stage/transaction_scheduler/greedy_scheduler.rs (L132-171)
```rust
        while budget > 0
            && num_scanned < self.config.max_scanned_transactions_per_scheduling_pass
            && !schedulable_threads.is_empty()
            && !container.is_empty()
        {
            let Some(id) = container.pop() else {
                unreachable!("container is not empty")
            };

            num_scanned += 1;

            // Should always be in the container, during initial testing phase panic.
            // Later, we can replace with a continue in case this does happen.
            let Some(transaction_state) = container.get_mut_transaction_state(id.id) else {
                panic!("transaction state must exist")
            };

            // Now check if the transaction can actually be scheduled.
            match try_schedule_transaction(
                transaction_state,
                &mut self.common.account_locks,
                schedulable_threads,
                |thread_set| {
                    select_thread(
                        thread_set,
                        self.common.batches.total_cus(),
                        self.common.in_flight_tracker.cus_in_flight_per_thread(),
                        self.common.batches.transactions(),
                        self.common.in_flight_tracker.num_in_flight_per_thread(),
                    )
                },
            ) {
                Err(TransactionSchedulingError::UnschedulableConflicts) => {
                    num_unschedulable_conflicts += 1;
                    self.unschedulables.push(id);
                }
                Err(TransactionSchedulingError::UnschedulableThread) => {
                    num_unschedulable_threads += 1;
                    self.unschedulables.push(id);
                }
```

**File:** core/src/banking_stage/transaction_scheduler/greedy_scheduler.rs (L228-229)
```rust
        // Push unschedulables back into the queue
        container.push_ids_into_queue(self.unschedulables.drain(..));
```
