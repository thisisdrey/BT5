### Title
Repository write-lock lease can expire and be stolen mid-transaction, reintroducing the ref-lock deadlock the serialization feature was built to prevent - ([File: internal/praefect/datastore/lock_manager.go])

### Summary
Praefect's `RepoReferenceWriteLockManager` serializes concurrent writes to the same repository using a PostgreSQL-backed lock with a fixed 20-second lease (`renewInterval`). The lease is only renewed when a Gitaly node reaches the `PREPARED_PHASE` or `COMMITTED_PHASE` of the reference-transaction hook [1](#0-0) . If the interval between an ordinary user's `PREPARING_PHASE` vote and its `PREPARED_PHASE` vote exceeds 20 seconds (e.g. a large push, slow disk, or contended node), the lease expires and can be atomically "stolen" by a second, unrelated transaction for the same repository via the lock manager's UPSERT query [2](#0-1) . This is analogous to the reported "execute before invalidating gate" pattern: the exclusivity check (lock acquisition) happens once up front, but the actual protected action (Git taking on-disk ref locks) can still be in flight when the gate silently expires and is handed to a different, concurrent actor, without any re-validation.

### Finding Description
The design doc explicitly states the purpose of this lock: serialize writes to a repository *before* any on-disk ref lock is taken, to avoid the classic two-node deadlock where T1 locks ref A on node 1 while T2 locks ref A on node 2 [3](#0-2) . The mechanism relies on the lock being held continuously across `PREPARING_PHASE` → `PREPARED_PHASE` → `COMMITTED_PHASE`.

However, the lock has an unconditional 20-second TTL, and the `tryLock` UPSERT allows any different transaction ID to take over the row once `expired_at < NOW()`, regardless of whether the original holder is still actively performing the guarded, non-idempotent Git ref-locking work [4](#0-3) :
```sql
ON CONFLICT (lock_id) DO UPDATE
  SET holder_txn_id = EXCLUDED.holder_txn_id, expired_at = EXCLUDED.expired_at
WHERE locks.expired_at < NOW() OR locks.holder_txn_id = $2
```
Renewal is driven purely by the votes actually arriving from Gitaly's hook (`PREPARED_PHASE`/`COMMITTED_PHASE`); there is no independent heartbeat tied to how long the underlying `git receive-pack`/ref-locking work is taking [5](#0-4) . Any legitimate slow-down between the `preparing` and `prepared` votes — a large push, a slow filesystem, GC/housekeeping contention, or simply many secondaries needing to reach quorum — can exceed the fixed 20s budget. Once that happens, `unlock`'s conditional `DELETE ... WHERE holder_txn_id = $2` becomes a no-op for the original holder, and a second push's `PREPARING_PHASE` vote can acquire the "expired" row and proceed to have Git take on-disk ref locks concurrently with the still-in-flight first transaction [6](#0-5) . This recreates precisely the cross-node ref-lock deadlock/race that the entire feature exists to eliminate.

### Impact Explanation
Two ordinary pushes to the *same* repository, one of which is merely slow, can end up racing to lock the same references on disk concurrently across replicas. This can reproduce the original deadlock scenario (mutually-waiting on-disk ref locks across nodes) or interleave ref updates in an order Praefect's quorum logic does not expect, risking stuck RPCs, inconsistent replicas, or repository corruption. Because this affects the write-serialization guarantee for a core, frequently-exercised code path (every mutating push through Praefect), the impact is a cluster-wide correctness/availability regression rather than a narrow edge case.

### Likelihood Explanation
No privileged access or malicious intent is required — the trigger is purely a matter of timing under legitimate use (large repositories, slow storage, high load causing quorum collection to take longer than the fixed lease). Since the 20-second interval is hard-coded and not adaptive to observed request latency, this can occur in production without any attacker involvement, making it a reasonably likely operational condition rather than a rare corner case.

### Recommendation
- Tie lock liveness to the actual duration of the protected critical section rather than a fixed external TTL disconnected from real progress — e.g., renew the lease as soon as the `PREPARING_PHASE` vote is cast and periodically while the RPC is still in flight, not only at the next hook phase boundary.
- Consider making the renew interval configurable and safely larger than realistic worst-case `preparing`→`prepared` latency, or implement a heartbeat from the still-executing Gitaly-side RPC handler independent of hook phase transitions.
- Add a safeguard so that stealing an "expired" lock while the original holder's RPC is still active is only possible after confirming (e.g., via a liveness/health check or generation check) that the original holder has actually terminated, not merely that its lease timer elapsed.

### Proof of Concept
1. Enable `PraefectSerializedWrite` and configure `RepoReferenceWriteLockManager`.
2. Client A pushes a large ref update to repository R; Gitaly casts the `PREPARING_PHASE` vote, acquiring the lock row for R with a 20s lease [2](#0-1) . Simulate/introduce a delay (e.g. slow filesystem, artificial sleep, or a genuinely large repository) so that more than 20 seconds elapse before the `PREPARED_PHASE` vote is cast.
3. Client B concurrently pushes to the same repository R and casts its own `PREPARING_PHASE` vote after the 20s window has elapsed; the UPSERT's `WHERE locks.expired_at < NOW()` clause matches and B "steals" the lock row while A's on-disk ref update is still executing [4](#0-3) .
4. Both A and B now proceed to take on-disk Git ref locks concurrently for the same repository/reference across replicas, reproducing the cross-node deadlock/race documented as the motivating problem in `doc/serialized_writes.md` [3](#0-2) .

### Citations

**File:** internal/praefect/transactions/manager.go (L272-306)
```go
func (mgr *Manager) lockRepoForTransaction(ctx context.Context, transactionID uint64, storageName, repoRelativePath string,
	phase gitalypb.VoteTransactionRequest_Phase,
) error {
	if featureflag.PraefectSerializedWrite.IsDisabled(ctx) {
		return nil
	}

	switch phase {
	case gitalypb.VoteTransactionRequest_PREPARING_PHASE:
		lock, err := mgr.repoWriteLockMgr.Lock(ctx, storageName, repoRelativePath, transactionID)
		if err != nil {
			return fmt.Errorf("try lock: %w", err)
		}
		mgr.repoLocks.Store(transactionID, lock)

	case gitalypb.VoteTransactionRequest_PREPARED_PHASE, gitalypb.VoteTransactionRequest_COMMITTED_PHASE:
		v, ok := mgr.repoLocks.Load(transactionID)
		if !ok {
			// No Preparing was cast for this transaction. This happens when Git itself drives
			// the reference-transaction hook on a Git build that doesn't emit "preparing"
			// (i.e. pre-2.54). Skip lock work; serialization is dormant for this path
			// until GIT_VERSION_PREV is bumped past 60d8c1e9.
			mgr.logger.WithFields(log.Fields{
				"transaction.id": transactionID,
				"phase":          phase.String(),
			}).DebugContext(ctx, "skipping lock check: no preparing phase recorded")
			return nil
		}
		lock := v.(datastore.RepoLock)
		if err := lock.Renew(ctx); err != nil {
			return err
		}
	}
	return nil
}
```

**File:** internal/praefect/datastore/lock_manager.go (L304-356)
```go
func (r *RepoReferenceWriteLockManager) tryLock(ctx context.Context, virtualStorage, relativePath string, txnID uint64,
) tryLockResult {
	lockID := repoLockID(virtualStorage, relativePath)
	// Register for a lock release notification before attempting the INSERT, so
	// that no release event can be missed between a failed attempt and the caller
	// beginning to wait, thus eliminating the race window between contention detection
	// and notification.
	notificationCh, deregister := r.handler.RegisterForLockRelease(lockID)

	query := `
INSERT INTO repository_reference_write_locks as locks (lock_id, holder_txn_id, expired_at)
VALUES ($1, $2, NOW() + $3::interval)
ON CONFLICT (lock_id) DO UPDATE
  SET holder_txn_id = EXCLUDED.holder_txn_id,
      expired_at    = EXCLUDED.expired_at
WHERE locks.expired_at < NOW() OR locks.holder_txn_id = $2
RETURNING lock_id, holder_txn_id, expired_at;`

	start := time.Now()
	rows, err := r.qc.QueryContext(ctx, query, lockID, txnID, r.renewInterval)
	r.operationDuration.WithLabelValues(virtualStorage, "trylock").Observe(time.Since(start).Seconds())
	if err != nil {
		r.lockAcquiredTotal.WithLabelValues(virtualStorage, "error").Inc()
		deregister()
		return tryLockResult{
			Acquired: false,
			Err:      fmt.Errorf("acquire repo reference write lock: %s, %w", lockID, err),
		}
	}
	defer func() {
		if err := rows.Close(); err != nil {
			r.logger.WithError(err).Error("close rows")
		}
	}()
	if rows.Next() {
		_, alreadyHeld := r.lockAcquiredAt.LoadOrStore(lockID, time.Now())
		if !alreadyHeld {
			r.locksHeld.WithLabelValues(virtualStorage).Inc()
			r.lockAcquiredTotal.WithLabelValues(virtualStorage, "new_acquisition").Inc()
		}
		deregister()
		unlockFn := func() error {
			return r.unlock(virtualStorage, relativePath, txnID)
		}
		renewFn := func(renewCtx context.Context) error {
			return r.renew(renewCtx, virtualStorage, relativePath, txnID)
		}
		return tryLockResult{
			Acquired: true,
			Unlock:   unlockFn,
			Renew:    renewFn,
		}
	}
```

**File:** internal/praefect/datastore/lock_manager.go (L373-399)
```go
// unlock releases the write lock held by txnID. It is a no-op if the lock is not currently held by txnID
// (e.g. it expired and was taken over by another transaction).
func (r *RepoReferenceWriteLockManager) unlock(virtualStorage string, relativePath string, txnID uint64) error {
	// unlock has its own context, because the lock must be released regardless of
	// the request's lifecycle. Don't use the caller's ctx because if the caller's request
	// context is cancelled (e.g. client disconnected, deadline exceeded), the request is
	// done but the lock row still exists in the database.
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	query := `
DELETE FROM repository_reference_write_locks
WHERE lock_id = $1 AND holder_txn_id = $2;`
	lockID := repoLockID(virtualStorage, relativePath)
	start := time.Now()
	_, err := r.qc.ExecContext(ctx, query, lockID, txnID)
	r.operationDuration.WithLabelValues(virtualStorage, "unlock").Observe(time.Since(start).Seconds())

	if err != nil {
		return fmt.Errorf("release repo reference write lock: %s, %w", lockID, err)
	}

	if acquiredAt, ok := r.lockAcquiredAt.LoadAndDelete(lockID); ok {
		r.locksHeld.WithLabelValues(virtualStorage).Dec()
		r.lockHoldDuration.WithLabelValues(virtualStorage).Observe(time.Since(acquiredAt.(time.Time)).Seconds())
	}
	return nil
```

**File:** doc/serialized_writes.md (L9-36)
```markdown
## Why Serialize Writes

The voting protocol relies on Git's `reference-transaction` hook to vote on a
ref update at each phase of the transaction. The phases Git historically
exposed are:

- `prepared` — references have been locked on disk
- `committed` — updates have been persisted
- `aborted` — the transaction was rolled back

Praefect collects votes from all Gitaly nodes participating in the transaction
and only allows the write to proceed when quorum is reached.

The problem is that at the `prepared` phase, **on-disk ref locks have already
been taken**. With two concurrent transactions T1 and T2 updating overlapping
refs A and B:

1. Node 1: T1 locks A, T2 locks B
1. Node 2: T2 locks A, T1 locks B

Each transaction is now waiting for the other to release a lock, and neither
can release until quorum is reached for its own lock. The cluster is
deadlocked.

The fix is to serialize transactions **before** any node has taken an on-disk
ref lock. If only one transaction per repository can advance past this point at
a time, lock acquisition order is globally consistent and the deadlock above
cannot occur.
```
