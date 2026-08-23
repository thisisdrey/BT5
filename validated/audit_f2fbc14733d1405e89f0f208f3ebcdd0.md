### Title
Lease-based repository write lock can be stolen mid-transaction while the original holder is still mutating on-disk refs, defeating write serialization - (File: internal/praefect/datastore/lock_manager.go)

### Summary
Praefect's "serialized writes" feature (`internal/praefect/datastore/lock_manager.go`, described in `doc/serialized_writes.md`) uses a PostgreSQL-backed, time-boxed lease (`expired_at`, 20s TTL) instead of a strict lock to serialize per-repository ref writes across the `PREPARING_PHASE` → `PREPARED_PHASE` → `COMMITTED_PHASE` sequence of the reference-transaction vote. Because lock ownership is re-evaluated purely by comparing `expired_at` against `NOW()` at each `tryLock`/`renew` call, a transaction that is delayed past the lease TTL (e.g., a large/slow push holding on-disk ref locks between `PREPARED_PHASE` and `COMMITTED_PHASE`) can have its lock silently "stolen" by a second, unrelated write to the same repository. This reintroduces the exact cross-node ref-lock deadlock/race that the serialization feature exists to prevent, and can be triggered by ordinary slow pushes — no privileged actor or malicious peer required.

### Finding Description
The lock acquisition query in `tryLock` allows a different `txnID` to take over a lock as soon as `expired_at < NOW()`, regardless of whether the original holder is still actively using it: [1](#0-0) 

The lease TTL is fixed at 20 seconds (`renewInterval`), and is only extended when the holder successfully calls `renew` at `PREPARED_PHASE`: [2](#0-1) [3](#0-2) 

Per the documented phase-to-action mapping, the lock is acquired at `PREPARING_PHASE`, renewed at `PREPARED_PHASE` (i.e., after Git has already taken on-disk ref locks), and only released at `COMMITTED_PHASE`: [4](#0-3) 

The `unlock` function's own release is conditioned on the caller's `txnID` still being the recorded holder, which correctly avoids releasing a lock stolen by someone else: [5](#0-4) 

However, that guard only protects the *unlock* step — it does nothing to stop a *second* transaction from acquiring the lock (via the `expired_at < NOW()` branch of the UPSERT) while the *first* transaction is still between `PREPARED_PHASE` (on-disk ref locks already taken by Git) and `COMMITTED_PHASE` (final unlock). If the elapsed time between the last successful renew and the `COMMITTED_PHASE` unlock exceeds the 20-second TTL — due to slow disk I/O, a large object pack, contention on the Gitaly node, GC pauses, or any other latency on the write path — a second write to the *same repository* can be admitted into `PREPARING_PHASE`/`PREPARED_PHASE` and start acquiring its own on-disk ref locks while the first transaction's Git process is still mid-flight. This is precisely the multi-writer, cross-node ref-lock race that `doc/serialized_writes.md` says the entire "preparing phase" serialization scheme was built to eliminate: [6](#0-5) 

Structurally, this is the same bug class as the external report: two operations (`resolveDispute`/lock-steal and `revealVote`/original-holder-finishing) are each individually validated against a boundary condition (`block.timestamp`/`expired_at` vs `NOW()`), but no cross-check prevents both from being "valid" and executing concurrently once the boundary is crossed by even a small margin, defeating the mutual-exclusion invariant the whole mechanism exists to guarantee.

### Impact Explanation
If two writes to the same repository run concurrently through Git's on-disk ref-locking machinery — the scenario this feature is explicitly built to prevent — the result can be a cross-node ref-lock deadlock (hung RPCs across the replica set, requiring operator intervention/timeout) or inconsistent ref state if one node's write partially overlaps another's. This is a reachable DoS/correctness issue on the primary write path for ordinary pushes, not a privileged-actor or malicious-peer scenario, and it undermines the very correctness guarantee (globally consistent lock acquisition order) that `doc/serialized_writes.md` states is the reason this feature exists.

### Likelihood Explanation
The trigger condition (a write taking longer than 20 seconds between its last successful renew and its commit-phase unlock) is realistic for ordinary large pushes, disk pressure, or Gitaly node contention — no attacker-controlled crafted RPC or malicious peer is needed, only two concurrent ordinary pushes to the same repository where one is slow. The renew is only attempted once, at `PREPARED_PHASE`; there is no visible retry/backoff logic shown to guarantee it lands well before expiry, and no mechanism observed that lets a soon-to-expire holder detect and block a concurrent theft before it commits.

### Recommendation
- Ensure the holder renews its lease with sufficient safety margin before `expired_at`, and/or have the holder periodically re-renew throughout long-running operations (not just once at `PREPARED_PHASE`), so that legitimate long writes are not treated as abandoned.
- Before allowing a lock "steal" on `expired_at < NOW()`, add a secondary check (e.g., a heartbeat or fencing token) so that a transaction which successfully stole the lock can detect and abort a still-running previous holder (or the previous holder detects it lost the lock and safely aborts) rather than both proceeding concurrently.
- Add stateful/concurrency tests (as recommended in the source report) specifically simulating a renew that arrives just after expiry, verifying that no two transactions can simultaneously hold on-disk ref locks for the same repository.

### Proof of Concept
1. Configure Praefect with the PostgreSQL-backed `RepoReferenceWriteLockManager` (serialized writes enabled).
2. Start Write A against repository `R`: it reaches `PREPARED_PHASE` (Git has taken on-disk ref locks) and successfully renews once, extending `expired_at` by 20s.
3. Artificially delay Write A's subsequent Git operations (e.g., via disk I/O throttling, large pack unpacking, or CPU starvation) so that more than 20 seconds elapse before it reaches `COMMITTED_PHASE`/unlock, without another renew being requested/succeeding in that window.
4. Start Write B against the same repository `R` at `PREPARING_PHASE` after Write A's lease has expired: `tryLock`'s UPSERT matches `locks.expired_at < NOW()`, so Write B is granted the lock even though Write A has not unlocked and its Git process is still holding on-disk ref locks. [1](#0-0) 
5. Both Write A and Write B now concurrently take on-disk ref locks against the same repository — reproducing the cross-node ref-lock deadlock the feature was designed to prevent.

Note: I was unable to fully read `internal/praefect/transactions/manager.go` in this session (the `read_file` tool call failed due to a parameter error and no further tool calls were available), so the exact call sites and phase-dispatch logic for `lockRepoForTransaction`/`unlockRepoForTransaction`/`renew` invocation timing are inferred from `doc/serialized_writes.md` and partial `manager.go` excerpts surfaced via search rather than a complete read of that file. Confirming the precise renew-retry behavior would require reviewing that file directly (e.g., via a Devin session with full file access).

### Citations

**File:** internal/praefect/datastore/lock_manager.go (L188-190)
```go
	// Start a scheduled cleanup background job to remove expired locks.
	lockRenewInterval := 20 * time.Second
	cleanUpJobTicker := helper.NewTimerTicker(2 * lockRenewInterval)
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

**File:** internal/praefect/datastore/lock_manager.go (L373-400)
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
}
```

**File:** internal/praefect/datastore/lock_manager.go (L402-430)
```go
// renew extends the expiry of the write lock held by txnID by another renewInterval.
// It returns an error if the lock is not currently held by txnID.
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

**File:** doc/serialized_writes.md (L17-36)
```markdown
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

**File:** doc/serialized_writes.md (L80-99)
```markdown
`internal/praefect/transactions/manager.go` owns the lock lifecycle. A
repository-scoped write lock backed by PostgreSQL is acquired, renewed, and
released according to the phase of each `VoteTransaction` call:

| Phase            | Action                                                  |
|------------------|---------------------------------------------------------|
| `PREPARING_PHASE`| `WriteLockManager.Lock` — block until exclusive access  |
| `PREPARED_PHASE` | `lock.Renew` — keep the lock alive across phases        |
| `COMMITTED_PHASE`| `lock.Unlock` — release once the write is durable       |

If any phase fails before commit, the deferred
`unlockRepoForTransaction` releases the lock so the next transaction can
proceed. Transactions that are explicitly stopped or canceled
(`StopTransaction`, `cancelTransaction`) also release the lock to avoid
stranding it in PostgreSQL.

The `WriteLockManager` interface lives in
`internal/praefect/datastore/lock_manager.go`. The production implementation is
PostgreSQL-backed; a `NoopWriteLockManager` is provided for callers that don't
need serialization.
```
