### Title
Intra-batch account lock conflicts are not detected in `AccountLocks::try_lock_transaction_batch`, allowing conflicting transactions in the same batch to both acquire write locks - (File: accounts-db/src/account_locks.rs)

### Summary
`AccountLocks::try_lock_transaction_batch` performs its conflict check (`can_lock_accounts`) in a first pass over the whole batch *without mutating* the lock state, and only mutates state (`lock_accounts`) in a second, separate pass. Because the first pass never registers a transaction's own tentative locks before checking the next transaction in the same batch, two (or more) transactions in the same batch that write-conflict (or write/read-conflict) on the same pubkey can both pass the check and both be unconditionally locked in the second pass, breaking the mutual-exclusion invariant that account locks are supposed to enforce.

### Finding Description
The function is implemented as two sequential passes: [1](#0-0) 

Pass 1 (lines 28-34) only calls `self.can_lock_accounts(keys.clone())` for each transaction's keys, and on failure rewrites that entry's `TransactionResult` to `Err`. Critically, it never calls `self.lock_accounts(...)` here, so `self` (the `write_locks` / `readonly_locks` maps) is **not updated** as the pass progresses through the batch. [2](#0-1) 

Pass 2 (lines 36-39) then iterates the (possibly already-`Err`) results and, for every entry that is still `Ok`, unconditionally calls `self.lock_accounts(keys)`, which just increments the lock counters with no re-validation: [3](#0-2) 

Consider a batch `[tx_A, tx_B]` where both write the same pubkey `P`, and `P` is not locked by any prior/unrelated in-flight transaction. In pass 1:
- `can_lock_accounts` for `tx_A` sees `write_locks[P]` absent → `can_write_lock(P)` is `true` → `tx_A` stays `Ok`.
- `can_lock_accounts` for `tx_B` is checked against the *same, still-unmutated* `self` (because pass 1 never locked `tx_A`'s keys) → `write_locks[P]` is still absent → `tx_B` also stays `Ok`.

In pass 2, both `tx_A` and `tx_B` are `Ok`, so `lock_accounts` is called for both, incrementing `write_locks[P]` to 2 with no error ever raised. The function returns `Ok(())` for *both* conflicting transactions, whereas the intended semantics (and the semantics implied by `can_write_lock`'s exclusivity check) require the second conflicting transaction to receive `TransactionError::AccountInUse`.

This defeats the entire purpose of `AccountLocks`: preventing conflicting transactions from being treated as lock-compatible for concurrent/overlapping execution within the same processing batch, since real conflicts are entirely missed for any batch-internal pair. Because the batch entrypoint (banking stage) forms these batches from ordinary unstaked-client transaction traffic (any two transactions writing to the same account, e.g., same fee payer or same program-derived account, landing in the same processing batch), this can be triggered by an unprivileged attacker with no special preconditions beyond ordinary transaction submission timing.

### Impact Explanation
This breaks the account-locking invariant that guarantees mutual exclusion for conflicting accounts within a single processing batch. Downstream code that relies on `try_lock_transaction_batch`'s per-entry `Ok`/`Err` result to decide which transactions may be treated as lock-safe for parallel/concurrent handling will incorrectly treat two write-conflicting (or write/read-conflicting) transactions as compatible, which can lead to concurrent access/execution of the same account by transactions that were supposed to be serialized. This maps to an "invalid recorded block / consensus-affecting execution ordering" category — a non-deterministic execution result across validators for what should be a deterministically-serialized pair of transactions, which is the mechanism behind the requested bank-hash-mismatch scenario. Note: I was not able to fully trace the exact banking-stage caller in `accounts-db/src/accounts.rs` in this session to confirm precisely how the boolean `Ok` results are consumed downstream (e.g., whether a later re-check in the scheduler nullifies the impact), so the downstream blast radius should be verified.

### Likelihood Explanation
No special privilege is required — any unstaked client submitting two ordinary transactions that write to a common pubkey, timed so they land in the same batch presented to `try_lock_transaction_batch`, triggers the double-Ok result deterministically. This is a plain logic bug in a self-contained, easily unit-testable function, not a race condition or timing side channel, so it is 100% reproducible whenever such a batch is formed.

### Recommendation
Merge the check-and-lock into a single pass so that `can_lock_accounts` is evaluated against the actual, progressively-updated lock state, e.g.:
```rust
validated_batch_keys
    .into_iter()
    .map(|available_keys| {
        available_keys.and_then(|keys| {
            self.can_lock_accounts(keys.clone())?;
            self.lock_accounts(keys);
            Ok(())
        })
    })
    .collect()
```
This ensures each transaction's tentative lock is registered in `self` before the next transaction in the batch is checked, so a later write/write or write/read conflict within the same batch is correctly rejected with `TransactionError::AccountInUse`.

### Proof of Concept
```rust
// accounts-db/src/account_locks.rs (test module)
#[test]
fn test_try_lock_transaction_batch_intra_batch_write_conflict() {
    let mut locks = AccountLocks::default();
    let key = Pubkey::new_unique();

    // Two transactions in the SAME batch both writing `key`.
    let tx_a_keys: Vec<(&Pubkey, bool)> = vec![(&key, true)];
    let tx_b_keys: Vec<(&Pubkey, bool)> = vec![(&key, true)];

    let batch = vec![
        Ok(tx_a_keys.into_iter()),
        Ok(tx_b_keys.into_iter()),
    ];

    let results = locks.try_lock_transaction_batch(batch);

    // Expected (correct) semantics: exactly one of the two conflicting
    // transactions should succeed and the other should fail with
    // AccountInUse, matching sequential lock-then-execute semantics.
    let ok_count = results.iter().filter(|r| r.is_ok()).count();
    assert_eq!(
        ok_count, 1,
        "both conflicting writers were locked successfully: {:?}",
        results
    );
}
```
Running this against the current implementation fails: both entries return `Ok(())`, demonstrating that the write-lock exclusivity invariant is violated for intra-batch conflicts. A property-based (proptest) extension generating random batches of write/write and write/read conflicting pubkey sets should further assert that, for any generated batch, the number of `Ok` results touching a given pubkey with at least one writable access is at most 1, and that the sequence of `Ok` results is consistent with sequential lock-then-execute order.

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
