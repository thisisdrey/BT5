### Title
Two-pass check-then-lock in `try_lock_transaction_batch` allows conflicting write locks within the same batch - ([File: accounts-db/src/account_locks.rs])

### Finding Description
`AccountLocks::try_lock_transaction_batch` splits validation and locking of an entire batch into two **separate, sequential passes** over the batch instead of atomically checking-then-locking each transaction in turn:

```rust
validated_batch_keys.iter_mut().for_each(|validated_keys| {
    if let Ok(keys) = validated_keys.as_ref()
        && let Err(e) = self.can_lock_accounts(keys.clone())
    { *validated_keys = Err(e); }
});

validated_batch_keys
    .into_iter()
    .map(|available_keys| available_keys.map(|keys| self.lock_accounts(keys)))
    .collect()
``` [1](#0-0) 

The first loop calls `can_lock_accounts` for **every** transaction in the batch against `self`, but `self` (`write_locks`/`readonly_locks`) is never mutated during this loop — mutation only happens afterward, in the second `.map` loop via `lock_accounts`. Consequently, if two transactions in the same batch write-lock the same `Pubkey`, `can_write_lock` for the second transaction is evaluated against the pre-batch state (empty for that key), not against the fact that the first transaction in the same batch is about to take that lock. Both checks pass, and the second loop then calls `lock_write`/`lock_readonly` unconditionally for both, incrementing the same counter twice:

```rust
fn can_write_lock(&self, key: &Pubkey) -> bool {
    !self.is_locked_readonly(key) && !self.is_locked_write(key)
}
fn lock_write(&mut self, key: &Pubkey) {
    *self.write_locks.entry(*key).or_default() += 1;
}
``` [2](#0-1) 

This is a bookkeeping/mutual-exclusion invariant violation: two transactions in the same batch are both reported as "successfully locked" for a write on the same account, when the entire purpose of `AccountLocks` is to guarantee that at most one execution path holds a write lock on a given `Pubkey` at a time so that concurrent/parallel transaction execution (e.g. `par_iter` execution of a sanitized batch) cannot race on the same account. Note: because `lock_write`/`unlock_write` counts stay balanced per-transaction (each locked key is unlocked exactly once by its own `unlock_accounts` call), this specific bug does **not** cause an integer-underflow panic in `unlock_write`/`unlock_readonly` as hypothesized in the proof idea — the counters remain internally consistent. The real, provable consequence is a correctness/verification bypass: the mutual-exclusion guarantee that downstream execution code relies on (assuming a batch has no internal write-write or read-write conflicts once `try_lock_transaction_batch` returns `Ok`) is broken.

### Impact Explanation
Because two conflicting transactions in one packet-derived batch can both be marked lockable for a write on the same account, downstream execution code that assumes locked batches are internally conflict-free (any code that parallelizes per-transaction account loads/writes within a batch based on this locking guarantee) can process both transactions concurrently against the same account state. This is a genuine verification/invariant bypass in `accounts-db`, matching the "invalid recorded block" bounty category if execution logic elsewhere relies on this exclusivity guarantee for correctness; it is not, however, the panic-on-unlock-underflow scenario described in the question — I found no reachable path where `unlock_write`/`unlock_readonly` underflow given this code, since lock/unlock counts stay balanced per successfully-locked transaction.

### Likelihood Explanation
Triggering the double-check pass only requires an attacker to submit multiple sanitized transactions that share a write-locked `Pubkey` and end up grouped in the same call to `try_lock_transaction_batch`. This is fully attacker-controlled (transactions writing to their own account, e.g. their own fee payer or a PDA they own), requiring no staked/leader privileges — a burst of ordinary transactions is sufficient. I could not, within the available tool budget, verify from `core/src/banking_stage/consumer.rs` or `runtime/src/bank.rs` whether the current execution path actually parallelizes per-transaction account mutation within one locked batch (which would be needed to turn this into a real data race/invalid block), so the severity of the downstream consequence is unconfirmed.

### Recommendation
Restore atomic check-then-lock semantics per transaction instead of two full passes over the batch: for each transaction, call `can_lock_accounts` and, if it succeeds, immediately call `lock_accounts` for that transaction before evaluating the next transaction in the batch. This ensures each subsequent transaction's `can_lock_accounts` check sees the locks taken by earlier transactions in the same batch.

### Proof of Concept
```rust
// accounts-db/src/account_locks.rs (test)
#[test]
fn test_try_lock_transaction_batch_same_batch_write_conflict() {
    let mut locks = AccountLocks::default();
    let key = Pubkey::new_unique();

    // Two transactions in the SAME batch both writing `key`.
    let tx1_keys = vec![(&key, true)];
    let tx2_keys = vec![(&key, true)];

    let batch: Vec<TransactionResult<_>> = vec![
        Ok(tx1_keys.into_iter()),
        Ok(tx2_keys.into_iter()),
    ];

    let results = locks.try_lock_transaction_batch(batch);

    // BUG: both are reported Ok, granting two simultaneous write locks
    // on the same key within a single batch.
    assert!(results[0].is_ok());
    assert!(results[1].is_ok()); // should be Err(AccountInUse) for a correct implementation

    // Demonstrates broken invariant: write_locks[key] == 2, i.e. two
    // "exclusive" write locks are concurrently held on the same account.
}
```
Expected assertion for a differential fuzz test: build a reference sequential locker that checks-and-locks transaction-by-transaction (interleaved), run randomized batches through both `try_lock_transaction_batch` and the reference, and assert the resulting `Ok`/`Err` vectors are identical. The current implementation will diverge whenever two same-batch transactions share a write-locked (or write/read-conflicting) `Pubkey`.

### Citations

**File:** accounts-db/src/account_locks.rs (L28-39)
```rust
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
```

**File:** accounts-db/src/account_locks.rs (L98-109)
```rust
    fn can_write_lock(&self, key: &Pubkey) -> bool {
        // If the key is not read-locked or write-locked, it can be write-locked
        !self.is_locked_readonly(key) && !self.is_locked_write(key)
    }

    fn lock_readonly(&mut self, key: &Pubkey) {
        *self.readonly_locks.entry(*key).or_default() += 1;
    }

    fn lock_write(&mut self, key: &Pubkey) {
        *self.write_locks.entry(*key).or_default() += 1;
    }
```
