### Title
Intra-batch account lock check/lock decoupling in `AccountLocks::try_lock_transaction_batch` allows conflicting writable locks to be granted concurrently within the same batch - (File: `accounts-db/src/account_locks.rs`)

### Summary
`try_lock_transaction_batch` validates every transaction in a batch against the *current* `AccountLocks` state in one pass, then unconditionally applies locks for all transactions that passed validation in a second pass. Because the validation pass never updates `self` between transactions, two (or more) transactions within the *same* batch that write to the same account both pass `can_lock_accounts` and are then both granted a write lock in the second pass, violating the mutual-exclusion invariant that account locks are supposed to enforce.

### Finding Description
`try_lock_transaction_batch` is implemented as two separate loops: [1](#0-0) 

The first loop (`validated_batch_keys.iter_mut().for_each(...)`) calls `self.can_lock_accounts(keys.clone())` for each transaction's key set, but `self` (the `write_locks`/`readonly_locks` maps) is not mutated by this loop — it is purely a read-only check against the pre-batch lock state.

Only in the second pass (`.into_iter().map(|available_keys| available_keys.map(|keys| self.lock_accounts(keys)))`) are locks actually written via `lock_accounts`/`lock_write`/`lock_readonly`: [2](#0-1) 

Consequently, if transaction A and transaction B in the same batch both write to account `X`, and `X` was unlocked before the batch started:
1. Pass 1: `can_lock_accounts` for A checks `self` (X unlocked) → `Ok`. `can_lock_accounts` for B also checks `self` (still unlocked, because A's lock hasn't been applied yet) → `Ok`.
2. Pass 2: `lock_accounts(A)` increments `write_locks[X]` to 1. `lock_accounts(B)` increments `write_locks[X]` to 2.

Both A and B end up simultaneously holding a "write lock" (via the reference-counted `write_locks` map) on the same account, even though `can_write_lock` is defined to require exclusive access (`!self.is_locked_readonly(key) && !self.is_locked_write(key)`, see lines 98-101). The reference-counting semantics of `write_locks`/`readonly_locks` (`*self.write_locks.entry(*key).or_default() += 1`) mean the structure is designed to tolerate multiple concurrent readers, but not multiple concurrent writers or a writer alongside readers — yet this intra-batch race lets exactly that happen for any batch containing internally-conflicting transactions.

This root-caused divergence is purely a function of the *order and content* of `validated_batch_keys` passed by the caller, so any code path that hands `try_lock_transaction_batch` a batch containing transactions with overlapping writable accounts — without external de-duplication — will silently grant conflicting locks instead of returning `TransactionError::AccountInUse` for the later conflicting transaction. If a batch producer (e.g., normal single-threaded bank/replay usage that expects `try_lock_transaction_batch` itself to be the source of truth for intra-batch conflict detection) relies on this function's return values to decide which transactions may execute concurrently in the same PoH entry, transactions with a shared writable account could be executed in parallel/without proper serialization, causing the recorded entry to not match a state consistent with sequential locked execution.

### Impact Explanation
If a caller relies on this function alone to prevent write-write or read-write conflicts within a batch (rather than pre-filtering intra-batch conflicts itself), the bug allows two conflicting transactions to be marked as successfully locked simultaneously. This breaks the fundamental account-locking invariant (`LOCK_CORRECTNESS`) that the accounts-db lock mechanism exists to guarantee, and could lead to concurrent unsynchronized execution against the same account, producing non-deterministic state and an entry that does not match the actually-executed transaction set — an invalid-block-class impact.

### Likelihood Explanation
No stake or privileged access is required to submit transactions; the vulnerability depends entirely on the *content* of a single batch (whether it contains transactions sharing a writable account) and is independent of attacker identity. However, I could not fully verify from the available code whether the actual batching path used by the banking stage/scheduler (e.g. `accounts-db/src/accounts.rs` callers, `scheduling-utils/src/thread_aware_account_locks.rs`) ever passes a batch containing intra-batch writable conflicts to this specific function, or whether upstream schedulers already guarantee conflict-free batches before calling it (in which case this bug would be latent/unreachable in practice). This caller-side guarantee could not be confirmed with the tools available in this session.

### Recommendation
Merge the check-and-lock into a single pass so that each transaction's lock is applied (or the transaction is rejected) before evaluating the next transaction's lock request, e.g., iterate once, and for each transaction call a combined `try_lock` that both checks against `self` and immediately locks on success, rolling forward the state before proceeding to the next transaction in the batch. Alternatively, explicitly deduplicate/validate for intra-batch conflicts before calling `try_lock_transaction_batch`, and add debug assertions verifying no double-write-lock scenario can be produced within a single batch call.

### Proof of Concept
```rust
// accounts-db/src/account_locks.rs (illustrative invariant test)
#[test]
fn test_try_lock_transaction_batch_intra_batch_conflict_not_detected() {
    let mut locks = AccountLocks::default();
    let key = Pubkey::new_unique();

    // Two transactions in the SAME batch both writing `key`.
    let batch: Vec<TransactionResult<_>> = vec![
        Ok(vec![(&key, true)].into_iter()),
        Ok(vec![(&key, true)].into_iter()),
    ];

    let results = locks.try_lock_transaction_batch(batch);

    // LOCK_CORRECTNESS invariant: at most one of the two conflicting
    // writable-lock transactions should succeed.
    let ok_count = results.iter().filter(|r| r.is_ok()).count();
    assert_eq!(
        ok_count, 1,
        "both conflicting write-lock transactions were granted locks concurrently"
    );
}
```
Expected (buggy) behavior: `ok_count == 2` because both transactions pass `can_lock_accounts` before either lock is applied, demonstrating the invariant violation described above.

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
