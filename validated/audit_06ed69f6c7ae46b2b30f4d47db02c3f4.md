### Title
Intra-batch account lock conflicts bypass `AccountLocks::try_lock_transaction_batch`, allowing concurrent execution of conflicting transactions in the same block - ([File: accounts-db/src/account_locks.rs])

### Summary
`AccountLocks::try_lock_transaction_batch` performs its conflict checks in a separate pass from lock application, so it only checks each transaction's keys against the lock state that existed *before* the batch call, never against locks that other transactions in the *same* batch will acquire. Two (or more) transactions in a single leader batch that read/write the same account can therefore all pass validation and all have their locks unconditionally applied, corrupting the `write_locks`/`readonly_locks` maps and permitting genuinely conflicting transactions to be executed concurrently.

### Finding Description
`try_lock_transaction_batch` is implemented as two independent loops: [1](#0-0) 

The first loop calls `can_lock_accounts` for every transaction against `&self` without mutating `self`: [2](#0-1) 

The second loop then unconditionally applies `lock_accounts` for every transaction that is still `Ok`, without re-checking conflicts: [3](#0-2) 

`lock_write`/`lock_readonly` simply increment counters with no guard: [4](#0-3) 

Because the check pass never sees the effect of locks taken by earlier transactions in the *same* batch (those locks are only applied in the second pass), two transactions in the batch that touch the same pubkey with conflicting access modes (write+write, write+read, or read+write) will both evaluate `can_lock_accounts` against the identical, unmodified pre-batch state and both return `Ok`. Both are then locked in the second pass: `write_locks[K]` can be incremented to 2, or `write_locks[K]` and `readonly_locks[K]` can both become non-zero simultaneously for the same key `K`, violating the fundamental invariant enforced elsewhere (`can_write_lock`/`can_read_lock`) that a key cannot be simultaneously write-locked and read/write-locked by another transaction.

`Accounts::lock_accounts` (the sole caller of `try_lock_transaction_batch`) only performs per-transaction validation (`validate_account_locks` — duplicate keys within one transaction and lock-count limit) before collecting the whole batch; it never checks for cross-transaction conflicts within the batch itself: [5](#0-4) 

This is reachable directly from banking stage's normal consume path — `Consumer::process_and_record_transactions_with_pre_results` calls `bank.prepare_sanitized_batch_with_results(txs, pre_results)`, which locks the whole batch of transactions in one call to `Accounts::lock_accounts`: [6](#0-5) 

An unprivileged attacker fully controls which transactions land in the same banking batch by submitting a burst of transactions (each independently signed by throwaway fee-payer keypairs to avoid `AlreadyProcessed`/dedup) that all write (or read/write) the same destination account. The existing test `test_bank_process_and_record_transactions_cost_tracker` documents the *intended* behavior — that a second transaction sharing a write-lock with the first in the same batch should be rejected with `AccountInUse` — but the current two-pass implementation of `try_lock_transaction_batch` does not enforce this for genuinely distinct (non-duplicate) transactions sharing a write-locked key, because both transactions are checked before either lock is applied.

### Impact Explanation
Once locking silently succeeds for conflicting transactions in the same batch, `load_and_execute_transactions` will process both transactions as if they were safely isolated, when in fact they touch the same account without any serialization guarantee from the lock manager. This breaks the "only one thread can mutate an account at a time" invariant documented directly above `Accounts::lock_accounts`: [7](#0-6) 

This can produce non-deterministic account state depending on scheduling/ordering, i.e., an invalid/inconsistent recorded block relative to what should be a strictly serialized execution model — a consensus-safety-relevant correctness bug, not merely a lock-leak. It also independently corrupts the `write_locks`/`readonly_locks` counters (e.g. `write_locks[K] == 2` or `write_locks[K] > 0 && readonly_locks[K] > 0` simultaneously), which can subsequently cause spurious `AccountInUse` rejections or lock accounting drift for unrelated future transactions on the same key.

### Likelihood Explanation
This requires no privileges beyond sending ordinary, validly-signed transactions to the leader's TPU: an attacker crafts N transactions from N distinct funded keypairs that all write (or a mix of read/write) the same target account, ensuring they land in the same consume batch. This is deterministic and trivially repeatable — no staking, no gossip control, and no more than the normal number of transaction submissions.

### Recommendation
Restore per-transaction atomic check-then-lock semantics: process the batch in a single loop that, for each transaction in order, calls `can_lock_accounts` and immediately calls `lock_accounts` (mutating `self`) before moving to the next transaction, so that later transactions in the batch observe locks already taken by earlier ones in the same batch. Do not split the check and apply phases into two full passes over the whole batch.

### Proof of Concept
```rust
// accounts-db/src/account_locks.rs (new test)
#[test]
fn test_try_lock_transaction_batch_intra_batch_write_conflict() {
    let mut account_locks = AccountLocks::default();
    let key = Pubkey::new_unique();

    // Two distinct "transactions" in the SAME batch call, both writing `key`.
    let tx0_keys = vec![(&key, true)];
    let tx1_keys = vec![(&key, true)];

    let batch = vec![
        Ok(tx0_keys.into_iter()),
        Ok(tx1_keys.into_iter()),
    ];

    let results = account_locks.try_lock_transaction_batch(batch);

    // Expected (correct) behavior: only one of the two conflicting writers
    // should succeed; the other must get AccountInUse.
    let ok_count = results.iter().filter(|r| r.is_ok()).count();
    assert_eq!(
        ok_count, 1,
        "both conflicting writers succeeded: {:?}",
        results
    );

    // Regardless, after unlocking all successes, the maps must be empty.
    for (result, keys) in results.iter().zip([vec![(&key, true)], vec![(&key, true)]]) {
        if result.is_ok() {
            account_locks.unlock_accounts(keys.into_iter());
        }
    }
    assert!(!account_locks.is_locked_write(&key));
    assert!(!account_locks.is_locked_readonly(&key));
}
```
Running this against the current implementation is expected to fail the `assert_eq!(ok_count, 1, ...)` assertion, demonstrating that both write-conflicting transactions in the same batch are incorrectly locked simultaneously.

### Citations

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

**File:** accounts-db/src/account_locks.rs (L56-71)
```rust
    fn can_lock_accounts<'a>(
        &self,
        keys: impl Iterator<Item = (&'a Pubkey, bool)>,
    ) -> TransactionResult<()> {
        for (key, writable) in keys {
            if writable {
                if !self.can_write_lock(key) {
                    return Err(TransactionError::AccountInUse);
                }
            } else if !self.can_read_lock(key) {
                return Err(TransactionError::AccountInUse);
            }
        }

        Ok(())
    }
```

**File:** accounts-db/src/account_locks.rs (L73-81)
```rust
    fn lock_accounts<'a>(&mut self, keys: impl Iterator<Item = (&'a Pubkey, bool)>) {
        for (key, writable) in keys {
            if writable {
                self.lock_write(key);
            } else {
                self.lock_readonly(key);
            }
        }
    }
```

**File:** accounts-db/src/account_locks.rs (L103-109)
```rust
    fn lock_readonly(&mut self, key: &Pubkey) {
        *self.readonly_locks.entry(*key).or_default() += 1;
    }

    fn lock_write(&mut self, key: &Pubkey) {
        *self.write_locks.entry(*key).or_default() += 1;
    }
```

**File:** accounts-db/src/accounts.rs (L452-454)
```rust
    /// This function will prevent multiple threads from modifying the same account state at the
    /// same time, possibly excluding transactions based on prior results
    #[must_use]
```

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

**File:** core/src/banking_stage/consumer.rs (L206-213)
```rust
        // Only lock accounts for transactions that passed pre-lock checks;
        // Once accounts are locked, other threads cannot encode transactions that will modify the
        // same account state
        let (batch, lock_us) =
            measure_us!(bank.prepare_sanitized_batch_with_results(txs, pre_results));

        let execute_and_commit_transactions_output =
            self.execute_and_commit_transactions_locked(bank, &batch, flags);
```
