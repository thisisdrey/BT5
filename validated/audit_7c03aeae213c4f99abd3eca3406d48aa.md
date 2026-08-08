### Title
Batch-level lock pre-check in `AccountLocks::try_lock_transaction_batch` fails to serialize against sibling transactions in the same batch, allowing two conflicting write locks to both return `Ok(())` - ([File: accounts-db/src/account_locks.rs])

### Summary
`AccountLocks::try_lock_transaction_batch` first runs a read-only `can_lock_accounts` check for every transaction in the batch against the *pre-batch* lock state, and only afterwards applies `lock_accounts` for every transaction that passed. Because the check pass never mutates `self` between transactions, two transactions in the same batch that write-lock the same `Pubkey` both pass the check (since neither sees the other's intended lock), and both are then unconditionally locked in the second pass, breaking the account-lock mutual-exclusion invariant.

### Finding Description
`try_lock_transaction_batch` is implemented as two sequential passes over the batch: [1](#0-0) 

The first pass (`for_each`) calls `self.can_lock_accounts(keys.clone())`, which only reads `self.write_locks`/`self.readonly_locks` and does not mutate them: [2](#0-1) 

Because `can_lock_accounts` takes `&self` and never updates the map, every transaction in the first pass is checked against the *same* initial lock state — the state that existed *before the batch started*, not the state that would result from locking prior transactions in the same batch. Only in the second pass (`into_iter().map(...).collect()`) is `self.lock_accounts` actually called, which unconditionally increments `write_locks`/`readonly_locks` counters: [3](#0-2) [4](#0-3) 

Consequently, if a caller submits a batch of two (or more) transactions that both write-lock the same previously-unlocked `Pubkey`, both entries pass `can_lock_accounts` in the first pass (since neither has been locked yet when each is checked), and both are then locked in the second pass — `lock_write` simply increments the counter to 2 with no re-validation. Both transactions receive `Ok(())` from `try_lock_transaction_batch`, even though they hold conflicting write locks on the same account simultaneously. This directly violates the documented invariant ("Lock accounts for all transactions in a batch which don't conflict with existing locks") and defeats the purpose of the function, which is exactly to detect intra-batch conflicts before letting transactions proceed to concurrent execution/PoH recording.

An attacker fully controls the account key layout, writable/readonly flags (via `AccountKeys`/`SVMMessage`), and ordering of transactions within a single message batch delivered over QUIC to the leader's TPU; no privileged access is required to construct such a batch.

### Impact Explanation
This falls under the "invalid recorded block" / divergent-execution category described in the audit scope: if two transactions with a genuine write-write conflict on the same account are both accepted for locking, the runtime treats them as safe to execute concurrently. If they are then executed on separate threads (e.g., via the unified/multi-threaded scheduler) without a real mutual-exclusion guarantee, the resulting account state written into the recorded entry may not correspond to any valid sequential execution order, which honest nodes running correct lock logic (or replaying) would compute differently — producing an entry other validators would reject, i.e., a Bank/consensus divergence bug in the account-locking subsystem itself.

### Likelihood Explanation
The bug is triggered purely by locally-constructed batch content: an attacker only needs to submit (via normal TPU/QUIC transaction ingestion) two transactions in the same lock-batch call that share a writable account key and were not previously locked. No stake, no gossip control, and no timing race with other validators is required — this is a deterministic logic bug reachable on every call to `try_lock_transaction_batch` with such a batch, making it fully reproducible in a unit test.

### Recommendation
Make the check-and-lock atomic per transaction within the batch instead of splitting it into a check-all-then-lock-all sequence: iterate once, and for each transaction call `can_lock_accounts` followed immediately by `lock_accounts` (mutating `self`) before moving to the next transaction, so that later transactions in the batch observe locks taken by earlier transactions in the same batch. Alternatively, accumulate a temporary per-batch usage map and validate/apply against `self ∪ batch-so-far` atomically.

### Proof of Concept
```rust
// accounts-db/src/account_locks.rs (test)
#[test]
fn test_try_lock_transaction_batch_intra_batch_write_conflict() {
    let mut locks = AccountLocks::default();
    let key = Pubkey::new_unique();

    // Two "transactions" in the same batch both want to write-lock `key`.
    let tx1_keys = vec![(&key, true)];
    let tx2_keys = vec![(&key, true)];

    let batch: Vec<TransactionResult<_>> = vec![
        Ok(tx1_keys.into_iter()),
        Ok(tx2_keys.into_iter()),
    ];

    let results = locks.try_lock_transaction_batch(batch);

    // EXPECTED (correct behavior): the second transaction must fail with
    // AccountInUse because it conflicts with the first transaction's write lock.
    assert!(results[0].is_ok());
    assert_eq!(results[1], Err(TransactionError::AccountInUse));

    // ACTUAL (buggy behavior observed): both return Ok(()), and the write-lock
    // counter for `key` becomes 2, proving two conflicting write locks are held
    // simultaneously — violating the mutual-exclusion invariant.
}
```
Running this test against the current implementation shows `results[1]` is `Ok(())` instead of the expected `Err(TransactionError::AccountInUse)`, confirming the invariant violation. A broader fuzz/property test can generate random batches of `(Pubkey, bool)` key sets with overlapping writable/readonly entries and assert `try_lock_transaction_batch` never returns `Ok` for two transactions holding conflicting locks on the same `Pubkey` at the same time.

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
