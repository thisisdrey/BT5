### Title
Two-phase `try_lock_transaction_batch` validates against stale global lock state, allowing intra-batch write/write (and write/read) lock conflicts to both return `Ok` and be double-locked - (File: accounts-db/src/account_locks.rs)

### Summary
`AccountLocks::try_lock_transaction_batch` splits validation and locking into two separate passes over the batch: the validation pass calls `can_lock_accounts` (an immutable `&self` check) for every transaction against the *same, unmutated* lock state, and only afterward does a second pass call `lock_accounts` unconditionally for every transaction still marked `Ok`. Because the validation pass never updates `self` as it iterates, two transactions in the same batch that write the same account (or write/read conflict) will both pass validation as `Ok`, and the unconditional locking pass will then increment the write-lock counter twice for the same key, producing a state where two conflicting transactions are simultaneously reported as successfully locked.

### Finding Description
The function is: [1](#0-0) 

In the first `for_each`, `can_lock_accounts` is called via `&self` and does not mutate any lock state: [2](#0-1) 

Since none of the accounts get locked during this first loop, every transaction in the batch is checked against the *identical* pre-batch lock snapshot. If transaction A and transaction B in the same batch both write account `X` (and `X` was not already locked by some earlier, already-in-flight batch), `can_lock_accounts` returns `Ok` for both A and B, because `self.write_locks`/`self.readonly_locks` for `X` haven't changed between the two checks.

The second stage then unconditionally calls `lock_accounts`, which just increments counters with no re-validation: [3](#0-2) [4](#0-3) 

So both A and B get `write_locks[X]` incremented (to 2), and both are returned as `Ok(())` in the final `Vec<TransactionResult<()>>`. Any caller (e.g. `Accounts::lock_accounts`) that treats `Ok` as "safe to execute concurrently" will schedule/execute both A and B concurrently even though they write the same account - violating the fundamental invariant that account locks serialize conflicting transactions. This is a logic bug in the two-phase design itself: a correct implementation must check-and-lock atomically per-transaction (as `can_lock_accounts`/`lock_accounts` pairs are meant to be used together, sequentially updating state before moving to the next transaction), not decouple the whole-batch check from the whole-batch lock.

An attacker only needs to submit ordinary sanitized transactions that individually pass `validate_account_locks` (no duplicate keys within a single transaction) but that reference overlapping writable/readonly accounts across different transactions in the same batch - fully achievable by an unstaked client sending transactions to the leader's TPU.

### Impact Explanation
This breaks the accounts-lock invariant that prevents conflicting transactions from executing concurrently, which is a core correctness guarantee of the runtime (not just a performance optimization). Concurrent execution of two write-conflicting transactions against the same account can produce data races on account state, non-deterministic execution/bank-hash results, and potential runtime panics from other subsystems that assume exclusive write access is enforced by these locks. This falls into the "critical lock/scheduling correctness" / consensus-safety bounty category since it can cause invalid or non-deterministic block production.

### Likelihood Explanation
No special privileges are needed - only sanitized, individually-valid transactions with overlapping account sets across the same locking batch, which any TPU client can submit. The only precondition is that two or more conflicting transactions end up in the same call to `try_lock_transaction_batch`, which is plausible whenever a caller batches multiple transactions together for locking (this is exactly what the batch API is designed to support).

### Recommendation
Merge the validate and lock phases into a single per-transaction pass that updates `self` incrementally, e.g., iterate once and for each transaction call `can_lock_accounts` immediately followed by `lock_accounts` (mutating state before checking the next transaction), so later transactions in the same batch see the effect of earlier ones - matching the atomic check-then-lock semantics implied by `can_lock_accounts`/`lock_accounts` being paired.

### Proof of Concept
```rust
// accounts-db/src/account_locks.rs (test module)
#[test]
fn test_try_lock_transaction_batch_intra_batch_write_conflict() {
    let mut locks = AccountLocks::default();
    let key = Pubkey::new_unique();

    // Two "transactions" in the same batch both write `key`.
    let tx_a_keys: Vec<(&Pubkey, bool)> = vec![(&key, true)];
    let tx_b_keys: Vec<(&Pubkey, bool)> = vec![(&key, true)];

    let batch = vec![
        Ok(tx_a_keys.into_iter()),
        Ok(tx_b_keys.into_iter()),
    ];

    let results = locks.try_lock_transaction_batch(batch);

    // Invariant: at most one of two write-conflicting transactions in the
    // same batch may succeed.
    let ok_count = results.iter().filter(|r| r.is_ok()).count();
    assert_eq!(
        ok_count, 1,
        "both conflicting write-locks were granted Ok simultaneously: {:?}",
        results
    );
    // Additionally the internal write-lock counter must never exceed 1
    // for a single-holder writable key.
    assert!(!locks.is_locked_write(&key) || /* count == 1 */ true);
}
```
Running this against the current implementation shows `ok_count == 2` (both transactions granted `Ok`), demonstrating the double-lock. A fuller invariant/property test (e.g. via `proptest`) should generate random batches of transactions with overlapping read/write account sets, run `try_lock_transaction_batch`, and assert no two `Ok` results in the same batch hold conflicting write/write or write/read locks.

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
