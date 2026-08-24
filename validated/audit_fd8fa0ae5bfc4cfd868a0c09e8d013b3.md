Confirmed root cause: `renew()` (`internal/praefect/datastore/lock_manager.go:404-430`) extends `expired_at` for a `holder_txn_id`-matched row with **no check that the lock has not already passed its `expired_at` deadline**. This is the direct analog of the Move `extend_expiration()` bug — a "renewal" function is callable by the current record-holder after its own deadline has already elapsed, racing against a concurrent party who is legitimately entitled to take over once the deadline passes.

### Title
Stale-holder lock renewal past expiry enables front-running takeover of a repository write lock - (File: internal/praefect/datastore/lock_manager.go)

### Summary
`RepoReferenceWriteLockManager.renew` (analogous to `extend_expiration()`) extends a repository write-lock's `expired_at` deadline based solely on `holder_txn_id` ownership, without verifying that the lock has not already expired. A delayed or GC/network-paused transaction can resurrect an already-expired lock and race a waiting transaction that is entitled to steal it, causing writes to be serialized incorrectly and potentially corrupting on-disk state or starving legitimate writers — the same "extend past grace period, front-run the rightful claimant" pattern as the reported Move bug.

### Finding Description
The lock is designed with an explicit, documented guarantee: `expired_at` is "the lock's deadline. If a holder crashes or never explicitly unlocks, another transaction can take the row over once it passes" [1](#0-0) .

`tryLock` correctly enforces this via an atomic UPSERT with the guard `WHERE locks.expired_at < NOW() OR locks.holder_txn_id = $2` [2](#0-1) . This means once `expired_at` has passed, any other `txnID` can legitimately steal the lock.

However, `renew` has no equivalent guard: [3](#0-2) 
The `UPDATE` only checks `lock_id = $1 AND holder_txn_id = $2` — it does not require `expired_at >= NOW()`. This means the *original* holder, even after its lease has already timed out, can still successfully call `renew` and push `expired_at` forward, exactly as long as no other transaction's `tryLock` UPSERT has yet won the row. This creates a race identical in structure to the reported bug: the "owner" (original holder, i.e., "Alice") can extend an already-expired lease and beat a waiting party ("Bob") that was entitled to take over once expiry passed, purely based on which SQL statement executes/commits first — there is no hard cutoff preventing the original holder from renewing past the deadline.

This is reachable from an ordinary user's push/write flow: any mutating RPC that goes through Praefect's `PREPARING_PHASE` acquires this lock and calls `Renew` periodically at `PREPARED_PHASE` while holding on-disk Git ref locks [4](#0-3) [5](#0-4) .

### Impact Explanation
If a holder's process/goroutine stalls beyond `renewInterval` (20s) — e.g. due to GC pause, slow DB round-trip, or transient network delay — the design intends for the lock to become stealable by a legitimately waiting transaction. Because `renew` doesn't check for prior expiry, the stalled holder can "wake up" and successfully renew, undoing the intended handover. Depending on timing, this can result in: two transactions momentarily believing they hold exclusive rights (one via `tryLock` steal, one via stale `renew`), and/or indefinite starvation of waiting writers if the stale holder is still failing/looping intermittently. This directly matches the accepted impact category "DoS of a handler" for the write-serialization path.

### Likelihood Explanation
Requires only ordinary conditions: a delayed goroutine (GC pause, DB latency spike, scheduler contention) plus concurrent write contention on the same repository, both realistic under production load with the 20-second TTL. No privileged access, malicious peer, or leaked token is required — it's triggered by normal write RPC traffic through Praefect's existing renew/steal race.

### Recommendation
Add an expiry guard to `renew`'s `WHERE` clause, e.g. `WHERE lock_id = $1 AND holder_txn_id = $2 AND expired_at >= NOW()`, so a holder cannot renew a lease that has already lapsed; treat a no-rows result (including the "already expired" case) as a definitive loss of the lock requiring the caller to re-acquire via `tryLock`.

### Proof of Concept
1. Transaction A acquires the lock via `tryLock`, `expired_at = now + 20s`.
2. Transaction A stalls (e.g., GC pause) for > 20s without calling `renew`.
3. Transaction B calls `tryLock`; the UPSERT's `WHERE expired_at < NOW()` matches, so B is positioned to steal.
4. Before B's UPSERT commits, A resumes and calls `renew`; since `renew` has no expiry check, it succeeds in extending `expired_at`, even though A's row was already past its deadline and eligible for takeover.
5. Depending on statement ordering, B's steal attempt may now fail (no rows returned) even though A's stall already violated the documented "another transaction can take the row over once it passes" guarantee, resulting in unexpected starvation/inconsistent handover for the waiting writer.

### Citations

**File:** doc/serialized_writes.md (L121-128)
```markdown
   casts a `Preparing` vote **before** locking refs locally. The hook RPC
   reaches Praefect, which calls `lockRepoForTransaction(PREPARING_PHASE)`:
   `WriteLockManager.Lock` blocks until the per-repository lock is free.
1. Both nodes return from `Preparing` together (quorum reached).
1. Each Gitaly node locks refs on disk and votes `Prepared`. Praefect renews
   the lock.
1. Each Gitaly node writes the update and votes `Committed`. Praefect releases
   the lock.
```

**File:** doc/serialized_writes.md (L170-177)
```markdown
- `lock_id` is formatted as `virtualStorage|relativePath`
  (see `repoLockID()` in `lock_manager.go`). One row per repository.
- `holder_txn_id` is the Praefect transaction ID currently owning the lock.
- `expired_at` is the lock's deadline. If a holder crashes or never explicitly
  unlocks, another transaction can take the row over once it passes.
- The index on `expired_at` supports the background sweep below.
- The trigger fires `PG_NOTIFY` on every `DELETE` so the listener in any
  Praefect instance can wake any waiter for that `lock_id`.
```

**File:** doc/serialized_writes.md (L244-255)
```markdown
**Renew** — `renew` extends the expiry of a held lock:

```sql
UPDATE repository_reference_write_locks
SET   expired_at = NOW() + $3::interval
WHERE lock_id = $1 AND holder_txn_id = $2
RETURNING expired_at;
```

If the row is gone (already stolen / unlocked), renew returns an error to the
caller. In the phase table above, renew is called at `PREPARED_PHASE` to keep
the lease alive while Git takes its on-disk ref locks.
```

**File:** internal/praefect/datastore/lock_manager.go (L313-320)
```go
	query := `
INSERT INTO repository_reference_write_locks as locks (lock_id, holder_txn_id, expired_at)
VALUES ($1, $2, NOW() + $3::interval)
ON CONFLICT (lock_id) DO UPDATE
  SET holder_txn_id = EXCLUDED.holder_txn_id,
      expired_at    = EXCLUDED.expired_at
WHERE locks.expired_at < NOW() OR locks.holder_txn_id = $2
RETURNING lock_id, holder_txn_id, expired_at;`
```

**File:** internal/praefect/datastore/lock_manager.go (L404-430)
```go
func (r *RepoReferenceWriteLockManager) renew(ctx context.Context, virtualStorage string, relativePath string, txnID uint64) error {
	query := `
UPDATE repository_reference_write_locks as locks
SET  expired_at = (NOW() + $3::interval)
WHERE locks.lock_id = $1 AND holder_txn_id = $2
RETURNING expired_at;
`
	lockID := repoLockID(virtualStorage, relativePath)
	start := time.Now()
	rows, err := r.qc.QueryContext(ctx, query, lockID, txnID, r.renewInterval)
	r.operationDuration.WithLabelValues(virtualStorage, "renew").Observe(time.Since(start).Seconds())
	if err != nil {
		return fmt.Errorf("renew repo reference write lock (executing query): %s, %w", lockID, err)
	}
	defer func() {
		if err := rows.Close(); err != nil {
			r.logger.WithError(err).Error("close rows")
		}
	}()
	if rows.Next() {
		return nil
	}
	if err := rows.Err(); err != nil {
		return fmt.Errorf("renew repo reference write lock (iterating rows): %s, %w", lockID, err)
	}
	return fmt.Errorf("renew repo reference write lock (no rows): %s", lockID)
}
```
