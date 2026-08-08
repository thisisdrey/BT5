### Title
Intra-batch write-lock conflicts are not detected by `AccountLocks::try_lock_transaction_batch`, allowing two conflicting transactions in the same batch to both acquire a write lock on the same account - (File: accounts-db/src/account_locks.rs)

### Summary
`AccountLocks::try_lock_transaction_batch` splits lock acquisition into two disjoint passes: a pure validation pass (`can_lock_accounts`) over *all* transactions in the batch, followed by an unconditional locking pass (`lock_accounts`) over the same set. Because the validation pass does not mutate lock state and therefore cannot see locks that will be taken by earlier transactions later in the same batch, two transactions in the same batch that write to the same account can both pass validation and both be granted a write lock, which is called from `Accounts::lock_accounts` at accounts-db/src/accounts.rs:455-474.

### Finding Description
`Accounts::lock_accounts` (accounts-db/src/accounts.rs:455-474) validates per-transaction account-key limits/duplicates via `validate_account_locks`, then hands the resulting iterators to `AccountLocks::try_lock_transaction_batch` (accounts-db/src/account_locks.rs:22-40) while holding `self.account_locks` mutex.

`try_lock_transaction_batch` does:
```rust
validated_batch_keys.iter_mut().for_each(|validated_keys| {
    if let Ok(keys) = validated_keys.as_ref()
        && let Err(e) = self.can_lock_accounts(keys.clone())
    { *validated_keys = Err(e); }
});

validated_batch_keys.into_iter()
    .map(|available_keys| available_keys.map(|keys| self.lock_accounts(keys)))
    .collect()
``` [1](#0-0) 

The first loop (`for_each`) calls `can_lock_accounts`, which is read-only and never mutates `self.write_locks`/`self.readonly_locks` [2](#0-1) . It is executed for *every* transaction in the batch before any lock is taken, so every check in this loop is evaluated against the pre-batch lock state only - it cannot observe conflicts from sibling transactions in the same call.

The second pass (`.map(...).collect()`) then unconditionally calls `self.lock_accounts(keys)` for every entry that was `Ok` after the first pass [3](#0-2) , with no re-check against locks already taken earlier in this same second pass. Consequently, if transaction A and transaction B in the same batch both write the same account (and neither conflicts with pre-existing external locks), both are marked `Ok` in the first pass and both get their write lock incremented in the second pass, and `try_lock_transaction_batch` returns `Ok(())` for both instead of `Err(AccountInUse)` for the second.

An unprivileged attacker only needs to submit two ordinary sanitized transactions that write the same account (e.g., both transferring lamports to/from the same account, or invoking the same program account) such that they end up in the same locking batch passed to `Accounts::lock_accounts`. Nothing upstream (sigverify, `validate_account_locks` per-transaction duplicate check, QoS) rejects this, because `validate_account_locks` (accounts-db/src/account_locks.rs:143-154) only checks duplicate keys *within a single transaction*, not across transactions in a batch [4](#0-3) .

### Impact Explanation
Both conflicting transactions would be reported as successfully locked and would proceed to execute as if non-conflicting, breaking the fundamental invariant that account locks serialize/ exclude concurrent execution of transactions touching the same account. If the batch is executed in parallel (multiple threads reading/mutating the same `AccountSharedData` concurrently), this can produce non-deterministic account state, lost updates (a form of double-spend/TOCTOU on lamport or program-state mutations), or a race that diverges from what the recorded PoH entry implies was executed serially. This falls under invalid recorded block / consensus-affecting non-determinism impact categories.

### Likelihood Explanation
No special privileges are required - an attacker submits two ordinary transactions writing the same account and relies on the leader's normal batching of pending transactions for lock+execute. The bug is purely in the shared, always-executed lock code path (`Accounts::lock_accounts` → `AccountLocks::try_lock_transaction_batch`), so it triggers whenever any batch happens to contain intra-batch write conflicts, which is a normal and expected scenario that this exact function is supposed to filter out per its own doc comment ("Lock accounts for all transactions in a batch which don't conflict with existing locks").

### Recommendation
Merge the validation and locking passes into a single sequential pass so that each transaction's lock check is evaluated against the account-lock state as mutated by every prior transaction already processed in the same batch (i.e., check-then-lock per transaction, not check-all-then-lock-all), restoring intra-batch conflict detection.

### Proof of Concept
Add to `accounts-db/src/account_locks.rs` tests module:
```rust
#[test]
fn test_try_lock_transaction_batch_intrabatch_write_conflict() {
    let mut locks = AccountLocks::default();
    let key = Pubkey::new_unique();

    // Two "transactions" in the same batch both write-lock the same key.
    let tx1_keys: Vec<(Pubkey, bool)> = vec![(key, true)];
    let tx2_keys: Vec<(Pubkey, bool)> = vec![(key, true)];

    let batch = vec![
        Ok(tx1_keys.iter().map(|(k, w)| (k, *w))),
        Ok(tx2_keys.iter().map(|(k, w)| (k, *w))),
    ];

    let results = locks.try_lock_transaction_batch(batch);

    // Expected (correct) behavior: second conflicting tx must fail.
    assert!(results[0].is_ok());
    assert_eq!(results[1], Err(TransactionError::AccountInUse));
}
```
Running this against the current implementation is expected to fail the second assertion (both entries return `Ok(())`), demonstrating that conflicting transactions within one batch are both granted the same write lock.

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

**File:** accounts-db/src/account_locks.rs (L143-154)
```rust
pub fn validate_account_locks(
    account_keys: AccountKeys,
    tx_account_lock_limit: usize,
) -> TransactionResult<()> {
    if account_keys.len() > tx_account_lock_limit {
        Err(TransactionError::TooManyAccountLocks)
    } else if has_duplicates(account_keys) {
        Err(TransactionError::AccountLoadedTwice)
    } else {
        Ok(())
    }
}
```
