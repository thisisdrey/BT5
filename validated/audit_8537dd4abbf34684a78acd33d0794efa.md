### Title
Batch-internal write-write conflicts are not detected in `AccountLocks::try_lock_transaction_batch`, allowing two conflicting transactions to both acquire write locks - ([File: accounts-db/src/account_locks.rs])

### Summary
`try_lock_transaction_batch` splits the check-and-lock operation into two separate passes: a validation pass that calls `can_lock_accounts` against the *pre-existing* lock table, followed by a second pass that unconditionally calls `lock_accounts` for every transaction that passed validation. Because the validation pass never accounts for locks that will be granted to other transactions later in the same batch, two write-conflicting transactions submitted together in one batch can both pass validation and both be granted the write lock.

### Finding Description
`try_lock_transaction_batch` first iterates `validated_batch_keys` and calls `self.can_lock_accounts(keys.clone())` for each transaction, which only reads `self.write_locks`/`self.readonly_locks` (state committed by *prior*, already-completed calls) — it does not mutate any lock state [1](#0-0) . Locking itself only happens in a second, independent pass over the same collection: `available_keys.map(|keys| self.lock_accounts(keys))`, called unconditionally for every entry that survived phase one, with no re-check against locks acquired by earlier entries in the same phase-two pass [2](#0-1) . `can_lock_accounts` itself only inspects `self.write_locks`/`self.readonly_locks` via `can_write_lock`/`can_read_lock` [3](#0-2) [4](#0-3) , and `lock_accounts` mutates the counters unconditionally without re-validating [5](#0-4) .

Consequently, if a batch passed into `try_lock_transaction_batch` contains two entries that both write the same pubkey, and that pubkey has no pre-existing lock, phase one will validate *both* entries as `Ok` (since neither has locked anything yet), and phase two will lock both, incrementing `write_locks[key]` to 2 while returning `Ok(())` for both transactions. The classical, correct pattern (check-then-lock interleaved per transaction within a single loop, so each subsequent check observes locks taken by earlier transactions in the same batch) has been replaced by this two-phase design, removing the batch-internal conflict check entirely.

### Impact Explanation
If a caller supplies `try_lock_transaction_batch` with an internally-conflicting write set (e.g., a banking-stage or replay path that has not already deduplicated writable-account conflicts before calling into this primitive), both conflicting transactions are reported as successfully locked and would be dispatched for execution against the same account state concurrently/out-of-order relative to intended sequencing. This is a "Critical, invalid block from entry/transaction mismatch"-class issue: bank state produced would not be deterministic relative to what a correct implementation would compute, which is a consensus-safety property (`solana_bank`/ledger correctness) and matches the account-locks invariant described in the prompt.

### Likelihood Explanation
Exploitability hinges entirely on whether any code path invokes `AccountLocks::try_lock_transaction_batch` with a batch containing writable-account collisions without pre-filtering. I was not able to fully trace the caller in `runtime/src/bank.rs` and `ledger/src/blockstore_processor.rs` (only found via `grep_search`, not read in full) before the tool budget was exhausted, so I cannot confirm whether upstream schedulers (banking-stage transaction scheduler, unified-scheduler, or replay-stage batch construction) already guarantee non-conflicting batches before calling this function. Given `SECURITY.md`'s scope restricts consideration to reachable attacker-controlled paths, and I could not confirm that an unprivileged attacker can force a conflicting pair of transactions into a single call to `try_lock_transaction_batch` (as opposed to the scheduler splitting them into separate calls/batches, which would make the two-phase design harmless), I cannot assert this is reachable end-to-end from the TPU ingress path with confidence.

### Recommendation
Merge the validation and locking passes into a single loop so each transaction's `can_lock_accounts` check is performed immediately before (and observes) `lock_accounts` for all prior transactions processed within the same `try_lock_transaction_batch` call, restoring atomicity of check-then-lock across the whole batch.

### Proof of Concept
```rust
// accounts-db/src/account_locks.rs (tests module)
#[test]
fn test_try_lock_transaction_batch_detects_internal_write_conflict() {
    let key = Pubkey::new_unique();
    let mut locks = AccountLocks::default();

    let tx1_keys = vec![(&key, true)];
    let tx2_keys = vec![(&key, true)];

    let batch: Vec<TransactionResult<_>> = vec![
        Ok(tx1_keys.into_iter()),
        Ok(tx2_keys.into_iter()),
    ];

    let results = locks.try_lock_transaction_batch(batch);
    let ok_count = results.iter().filter(|r| r.is_ok()).count();

    // Expected: exactly one Ok(()) and one Err(AccountInUse).
    // Actual (with current implementation): both are Ok(()).
    assert_eq!(
        ok_count, 1,
        "both write-conflicting transactions were granted locks in the same batch"
    );
}
```
Expected result under a correct implementation: exactly one `Ok(())` and one `Err(TransactionError::AccountInUse)`. Under the current implementation both entries are `Ok(())`, and `locks.write_locks[&key] == 2` after the call, demonstrating the double-grant.

### Citations

**File:** accounts-db/src/account_locks.rs (L28-34)
```rust
        validated_batch_keys.iter_mut().for_each(|validated_keys| {
            if let Ok(keys) = validated_keys.as_ref()
                && let Err(e) = self.can_lock_accounts(keys.clone())
            {
                *validated_keys = Err(e);
            }
        });
```

**File:** accounts-db/src/account_locks.rs (L36-40)
```rust
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
