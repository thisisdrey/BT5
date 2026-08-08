### Title
`try_lock_transaction_batch` fails to detect write-lock conflicts between transactions within the same batch, allowing concurrent execution of conflicting transactions - ([File: accounts-db/src/account_locks.rs])

### Summary
`AccountLocks::try_lock_transaction_batch` performs a two-pass algorithm: it first checks every transaction's account keys against the *pre-batch* lock state, and only afterward applies the locks for all transactions that passed. Because the conflict check for every transaction in the batch runs before any transaction in that same batch has actually taken its locks, two (or more) transactions within one batch that write the same account will both pass the check and both be granted the lock, violating the exclusive-write-lock invariant that `Accounts::lock_accounts` is supposed to enforce.

### Finding Description
`Accounts::lock_accounts` validates each transaction's account keys (rejecting per-transaction duplicates and over-limit account counts via `validate_account_locks`) and then hands the validated key lists to `AccountLocks::try_lock_transaction_batch` while holding the single `account_locks` mutex: [1](#0-0) 

`try_lock_transaction_batch` is implemented as two separate passes over `validated_batch_keys`: [2](#0-1) 

The first pass (`iter_mut().for_each`) calls `self.can_lock_accounts(keys.clone())` for every transaction in the batch, but this check is evaluated against `self.write_locks` / `self.readonly_locks` as they existed *before the batch started* — none of the batch's own transactions have taken any lock yet at this point. Only in the second pass (`into_iter().map(...).collect()`) are locks actually applied via `self.lock_accounts(keys)`, which unconditionally increments `write_locks`/`readonly_locks` counters with no re-check against locks taken earlier in the same second pass: [3](#0-2) [4](#0-3) 

Consequently, if transaction A and transaction B in the same batch both write to account `X` (and `X` was not already locked by an outside batch), both pass the check phase (since neither has locked `X` yet when checked), and both then successfully acquire the write lock on `X` in the apply phase — `write_locks[X]` simply becomes `2` instead of the second attempt returning `Err(AccountInUse)`. Both transactions are returned as `Ok(())` from `lock_accounts`, meaning callers (banking stage / replay) will treat them as non-conflicting and may schedule them for concurrent execution in the same batch.

An unprivileged attacker only needs to submit two ordinary sanitized transactions that write to the same account (no duplicate keys within a single transaction — that path is already correctly rejected by `validate_account_locks`/`has_duplicates`) such that they land in the same lock batch. This does not require any duplicate-key-per-transaction trick nor hitting the `MAX_TX_ACCOUNT_LOCKS` boundary specifically; the boundary condition mentioned in the question is not actually what triggers the bug — the two-pass check/apply split is the root cause, independent of `tx_account_lock_limit`.

### Impact Explanation
This breaks the core account-locking invariant that "account locks prevent conflicting transactions from executing concurrently." If a leader's batch scheduler dispatches transactions with `Ok` lock results to separate execution threads, two transactions writing the same account could execute concurrently, producing a non-deterministic result for that account depending on thread interleaving. Since PoH/consensus assumes a single deterministic execution order enforced by exclusive locks, this can cause the resulting block state to diverge from what other validators (replaying the same transactions serially, or with correct lock semantics) would compute, i.e., an invalid/inconsistent recorded block.

### Likelihood Explanation
Feasibility is high in principle: any two transactions writing a shared account (e.g., both transferring from the same source account) submitted in the same batch trigger the described check/apply gap. The only real precondition is that both transactions actually land in the same batch passed to a single `lock_accounts` call, which is influenced by batching logic outside this function.

### Recommendation
Change `try_lock_transaction_batch` to check-and-lock each transaction incrementally in a single pass (check `can_lock_accounts`, and if it succeeds, immediately call `lock_accounts` before moving to the next transaction), so that each transaction's conflict check sees the locks already taken by earlier transactions in the same batch.

### Proof of Concept
```rust
// accounts-db/src/account_locks.rs (or accounts-db/src/accounts.rs), new test
#[test]
fn test_try_lock_transaction_batch_intrabatch_write_conflict() {
    let key = Pubkey::new_unique();
    let mut account_locks = AccountLocks::default();

    // Two "transactions" in the same batch both writing `key`.
    let batch: Vec<TransactionResult<_>> = vec![
        Ok(vec![(&key, true)].into_iter()),
        Ok(vec![(&key, true)].into_iter()),
    ];

    let results = account_locks.try_lock_transaction_batch(batch);

    // EXPECTED (per lock invariant): only the first transaction should succeed;
    // the second should fail with AccountInUse because it conflicts with the
    // first transaction in the same batch.
    assert!(results[0].is_ok());
    assert_eq!(results[1], Err(TransactionError::AccountInUse)); // currently FAILS: both are Ok
}
```
This test, run against the current implementation, demonstrates that `results[1]` is `Ok(())` instead of the expected `Err(TransactionError::AccountInUse)`, confirming that intrabatch write-write conflicts are not detected. A fuzz extension can permute N transactions each referencing overlapping subsets of a small key pool (with total unique keys near `MAX_TX_ACCOUNT_LOCKS`) and assert that for every pair of transactions sharing a writable (or write/read) key within the same batch, at most one receives `Ok(())`.

### Citations

**File:** accounts-db/src/accounts.rs (L463-474)
```rust
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
