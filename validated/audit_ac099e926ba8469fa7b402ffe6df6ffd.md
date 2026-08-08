### Title
Intra-batch double write-lock grant due to check-then-lock phase separation in `try_lock_transaction_batch` - ([File: accounts-db/src/account_locks.rs])

### Summary
`AccountLocks::try_lock_transaction_batch` splits its work into two full passes over the whole batch: a first pass that calls `can_lock_accounts` for every transaction, and only afterward a second pass that actually calls `lock_accounts`. Because the first pass never mutates `self.write_locks`/`self.readonly_locks`, two (or more) transactions in the *same* batch that write the same account both pass the `can_lock_accounts` check, and then both get locked in the second pass with no re-check, resulting in a write-lock counter of 2 (or more) on a single account and both transactions returning `Ok(())`.

### Finding Description
`try_lock_transaction_batch` [1](#0-0)  does:

1. Phase 1 — for every transaction in `validated_batch_keys`, call `self.can_lock_accounts(keys.clone())` which only reads `self.write_locks`/`self.readonly_locks` via `&self` [2](#0-1) . No lock state is updated during this loop.
2. Phase 2 — only after all checks are done, iterate again and call `self.lock_accounts(keys)` for every transaction that passed, which unconditionally increments the write/read counters via `lock_write`/`lock_readonly` with no conflict re-check [3](#0-2) .

Because phase 1's checks are evaluated entirely against the pre-batch lock state (locks from *outside* this batch), any two transactions inside the same batch that write the same account both observe `can_write_lock(key) == true` and both pass. Phase 2 then applies `lock_write` twice for the same key, incrementing the counter to 2, with both transactions reported as `Ok(())`. This violates the intended invariant that `AccountLocks` prevents two write-locks (or a write-lock and read-lock) from being held on the same account simultaneously, which is the entire purpose of `can_write_lock`/`can_read_lock`.

An unprivileged attacker only needs to submit a packet batch of transactions with overlapping writable account keys (e.g., same fee-payer or same target account across multiple crafted transactions) to a leader's TPU; nothing in the shown code de-duplicates conflicting accounts across a single batch before calling `try_lock_transaction_batch`, and the per-transaction sanitization (`validate_account_locks`) only rejects duplicate keys *within a single transaction*, not across transactions in a batch [4](#0-3) .

### Impact Explanation
If two transactions are granted a write lock on the same account simultaneously, any downstream logic that treats "successfully locked" as a guarantee of exclusive access (e.g., dispatching both to be executed/loaded concurrently) can process the same account without serialization, producing a bank state that diverges from the deterministic serial execution order implied by the entry. This is a correctness/consensus-safety violation against the account-locking invariant that Agave relies on to allow safe parallel transaction execution, and could manifest as an invalid recorded block or non-deterministic execution result depending on how callers consume the lock results.

### Likelihood Explanation
The only precondition is crafting many transactions with overlapping (conflicting) writable accounts inside a single packet batch — trivially achievable by any unstaked remote client sending transactions to the leader's TPU port, since the vulnerable two-phase check happens purely inside `AccountLocks` with no cross-transaction de-duplication guard. The bug is deterministic (not timing-dependent) and reproducible with a small, direct unit test against the function.

### Recommendation
Interleave the check-then-lock steps per transaction instead of doing two full passes: for each transaction, call `can_lock_accounts` immediately followed by `lock_accounts` before moving to the next transaction in the batch, so that each subsequent transaction's check sees the locks already applied by earlier transactions in the same batch.

### Proof of Concept
```rust
// accounts-db/src/account_locks.rs (test module)
#[test]
fn test_try_lock_transaction_batch_intra_batch_double_write_lock() {
    let key = Pubkey::new_unique();
    let mut locks = AccountLocks::default();

    // Two transactions in ONE batch call, both writing the same key.
    let batch: Vec<TransactionResult<_>> = vec![
        Ok(vec![(&key, true)].into_iter()),
        Ok(vec![(&key, true)].into_iter()),
    ];

    let results = locks.try_lock_transaction_batch(batch);

    // BUG: both succeed even though they write-conflict on `key`.
    assert!(results[0].is_ok());
    assert!(results[1].is_ok()); // should be Err(AccountInUse) but isn't

    // Internal state now has a double write-lock on the same key,
    // which should never happen (`can_write_lock` is supposed to
    // guarantee exclusivity).
    // (requires `dev-context-only-utils` feature to inspect state)
    assert!(locks.is_locked_write(&key));
}
```
Expected (fixed) behavior: the second transaction should receive `Err(TransactionError::AccountInUse)` because the first transaction's write lock on `key` should already be visible when the second transaction is checked.

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

**File:** accounts-db/src/account_locks.rs (L73-109)
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

    #[cfg_attr(feature = "dev-context-only-utils", qualifiers(pub))]
    fn is_locked_readonly(&self, key: &Pubkey) -> bool {
        self.readonly_locks.get(key).is_some_and(|count| *count > 0)
    }

    #[cfg_attr(feature = "dev-context-only-utils", qualifiers(pub))]
    fn is_locked_write(&self, key: &Pubkey) -> bool {
        self.write_locks.get(key).is_some_and(|count| *count > 0)
    }

    fn can_read_lock(&self, key: &Pubkey) -> bool {
        // If the key is not write-locked, it can be read-locked
        !self.is_locked_write(key)
    }

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

**File:** accounts-db/src/account_locks.rs (L142-154)
```rust
/// Validate account locks before locking.
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
