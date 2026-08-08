### Title
Intra-batch account lock conflicts are not detected in `AccountLocks::try_lock_transaction_batch`, allowing mutually-exclusive read/write locks on the same account to all succeed within one batch - ([File: accounts-db/src/account_locks.rs])

### Summary
`try_lock_transaction_batch` validates every transaction in a batch against the lock state that existed *before* the batch started, then applies all successful locks in a second pass, without re-checking against locks acquired by earlier transactions in the same batch. This lets multiple conflicting entries (e.g. two writers, or a writer and a reader, on the same account) all pass validation and all get "locked," corrupting the write/read lock counters and breaking the mutual-exclusion invariant the lock system exists to enforce.

### Finding Description
`try_lock_transaction_batch` performs two sequential passes over `validated_batch_keys`: [1](#0-0) 

Pass 1 (validation) calls `self.can_lock_accounts(keys.clone())` for each batch entry. `can_lock_accounts` only reads `self.write_locks`/`self.readonly_locks` — it does not mutate `self` in any way: [2](#0-1) 

Because pass 1 never mutates `self`, every entry in the batch is checked against the *same* pre-batch lock state, regardless of what other entries in the batch requested. Pass 2 then unconditionally applies `self.lock_accounts(keys)` (which does mutate `write_locks`/`readonly_locks` via `lock_write`/`lock_readonly`) for every entry that was marked `Ok` in pass 1: [3](#0-2) 

Concretely, given the batch from the question — tx1 write-locks Y, tx2 read-locks Y, tx3 write-locks Y, and Y is unlocked before the batch — pass 1 evaluates `can_lock_accounts` for tx1, tx2, and tx3 independently against the *unlocked* state of Y (since pass 1 never calls `lock_write`/`lock_readonly`). All three checks see `can_write_lock(Y) == true` / `can_read_lock(Y) == true` and all three are marked `Ok`. Pass 2 then calls `lock_write(Y)` for tx1 (`write_locks[Y] = 1`), `lock_readonly(Y)` for tx2 (`readonly_locks[Y] = 1`), and `lock_write(Y)` for tx3 (`write_locks[Y] = 2`) — all unconditionally, because pass 2 no longer performs any conflict check, it simply applies whatever pass 1 decided. The result is a state where a write lock and a read lock coexist on the same account, and two independent write locks coexist on the same account, violating the fundamental `can_write_lock`/`can_read_lock` invariant (`!is_locked_readonly(key) && !is_locked_write(key)` for writers, `!is_locked_write(key)` for readers) that this very same module enforces for *cross-batch* locking.

This is a genuine logic bug in the two-phase design: the validation phase is meant to reflect "would this succeed if locks were taken in order," but it computes that answer against a stale snapshot of `self` rather than incorporating the tentative effects of earlier same-batch entries.

### Impact Explanation
This corrupts the account-locking invariant that guarantees mutual exclusion between conflicting transactions. If reachable from a batch built out of attacker-controlled account key sets (accounts-db exposes no built-in same-batch dedup — `validate_account_locks`/`has_duplicates` only reject duplicate keys *within a single transaction*, not across transactions in a batch), the runtime would believe two conflicting transactions (e.g., two writers of the same account) have both been granted exclusive locks and could dispatch them for concurrent execution against the same account state — a correctness/consensus-safety violation (concurrent conflicting writes to account state, or a reader observing state concurrently with an in-flight writer), matching the "invalid recorded block / execution correctness" bounty category. It would also corrupt the internal lock reference counts (e.g. `write_locks[Y] = 2` after only one transaction has actually been unlocked), potentially leading to accounts remaining incorrectly locked (denial of subsequent legitimate transactions) or incorrectly unlocked, once unlock bookkeeping desyncs from the true number of holders.

### Likelihood Explanation
The trigger condition is purely a data/ordering property of a single batch and requires no privileged access — merely constructing a batch (via ordinary transaction submission such that batching groups conflicting transactions together) where two or more transactions reference the same account with conflicting lock modes. Whether this is externally reachable depends entirely on how the scheduler/bank constructs the `validated_batch_keys` argument to `try_lock_transaction_batch` and whether it already deduplicates conflicting keys across transactions in the same batch before calling this function; that call-site logic was not fully inspected in this pass, so likelihood as an end-to-end externally triggerable defect is not fully confirmed, but the function itself, in isolation, does not protect against this input and is documented to return "a vector of `TransactionResult` indicating success or failure for each transaction," implying per-transaction correctness is expected of it regardless of caller behavior.

### Recommendation
Make pass 1 apply tentative locks (or otherwise track keys already claimed within the batch) so that each subsequent entry's `can_lock_accounts` check accounts for locks decided earlier in the same batch, and roll back or replay properly on failure — e.g., merge the two passes into a single pass that calls `can_lock_accounts` then immediately `lock_accounts` per entry (in order), only skipping the lock step for entries that fail, so state is mutated incrementally and later entries see the true up-to-date lock state.

### Proof of Concept
```rust
// accounts-db/src/account_locks.rs (added test)
#[test]
fn test_try_lock_transaction_batch_intra_batch_conflict() {
    let mut locks = AccountLocks::default();
    let y = Pubkey::new_unique();

    // tx1: write Y, tx2: read Y, tx3: write Y — all in ONE batch, Y unlocked before batch.
    let batch: Vec<TransactionResult<_>> = vec![
        Ok(vec![(&y, true)].into_iter()),  // tx1 write
        Ok(vec![(&y, false)].into_iter()), // tx2 read
        Ok(vec![(&y, true)].into_iter()),  // tx3 write
    ];

    let results = locks.try_lock_transaction_batch(batch);

    // Oracle: sequential locking would give tx1=Ok, tx2=Err(AccountInUse), tx3=Err(AccountInUse)
    // (or some ordering-consistent single-writer outcome), never all three Ok.
    let ok_count = results.iter().filter(|r| r.is_ok()).count();
    assert!(
        ok_count <= 1,
        "expected at most one lock winner for conflicting writers/reader on Y, got {:?}",
        results
    );

    // Additionally demonstrate corrupted internal state if the bug is present:
    assert!(
        !(locks.is_locked_write(&y) && locks.is_locked_readonly(&y)),
        "write and read lock coexist on same key — mutual exclusion invariant broken"
    );
}
```
Running this against current `try_lock_transaction_batch` fails both assertions: all three results are `Ok(())`, and both `is_locked_write(&y)` and `is_locked_readonly(&y)` are true simultaneously, demonstrating the invariant break.

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
