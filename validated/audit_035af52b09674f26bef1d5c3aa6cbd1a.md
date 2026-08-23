### Title
Unbounded, unpaid, non-preemptible per-repository write lock in Praefect's serialized-writes feature allows any single push to DoS all other writes to that repository - (File: `internal/praefect/transactions/manager.go`, `internal/praefect/datastore/lock_manager.go`)

### Summary
Praefect's serialized-writes feature (`PraefectSerializedWrite` flag) grants the *first* reference-transaction that reaches the `PREPARING_PHASE` an exclusive, repository-wide write lock via `WriteLockManager.Lock`, holds it through `PREPARED_PHASE` (renewed), and releases it only at `COMMITTED_PHASE`. Any unprivileged, authenticated pusher can acquire this lock for free and hold it — via repeated, cost-free `Renew` calls — for as long as their own push takes to process, during which **every other client's write to the same repository is queued with no way to preempt, outbid, or prioritize**. This mirrors the referenced smart-contract bug class: a trivially-cheap actor action creates an exclusive lock that indiscriminately blocks all other legitimate actors until it naturally expires, with no override mechanism.

### Finding Description
`internal/praefect/transactions/manager.go`'s `lockRepoForTransaction` acquires the per-repository lock at `PREPARING_PHASE`: [1](#0-0) 

and renews it at `PREPARED_PHASE`, releasing only at `COMMITTED_PHASE` or on failure/cancellation: [2](#0-1) 

The lock itself is a single row keyed by `virtualStorage|relativePath` in `internal/praefect/datastore/lock_manager.go`, and its `renew` function simply pushes the expiry forward by `renewInterval` (20s) every time it is called, with no cap on the number of renewals or total hold duration: [3](#0-2) 

Any other client attempting to write to the same repository blocks in `tryLock`/`Lock`, waiting on a release notification with no override path: [4](#0-3) 

As documented, the lock is "acquired, renewed, and released according to the phase of each `VoteTransaction` call," and "writes to the same repository ... now run one at a time": [5](#0-4) [6](#0-5) 

There is no minimum stake, priority, or fee to acquire the lock — any ordinary push (an unprivileged, reachable RPC path from the user's perspective) obtains it for free, exactly like the referenced bug where a buyout could be started with "as little as 1 wei." Unlike the buyout module (which at least has a fixed multi-day timer), this lock's lifetime is bounded only by however long the holder's own push takes to reach the `Committed` phase, and is actively extendable via `Renew` for as long as the transaction remains alive — so a deliberately slow or large push (e.g., large ref/pack processing, slow disk I/O, or an intentionally engineered delay between `Preparing`/`Prepared` votes) can hold the whole-repository lock far longer than the nominal 20-second TTL, since `Renew` resets the clock on every call with no maximum.

### Impact Explanation
While the lock is held, **all other legitimate pushes to the same repository are queued indefinitely** with no way to jump the queue, cancel the holder, or offer a "higher priority" write — a repository-wide denial of service for any concurrent writer, achievable by any authenticated user who can push to the repo. This can be used to block legitimate, time-sensitive pushes (e.g., front-running a specific push/release) or to generally degrade repository availability, for a duration entirely controlled by the attacker's own push characteristics rather than any fixed, bounded window.

### Likelihood Explanation
The serialized-writes path is reachable by any normal push once `PraefectSerializedWrite` is enabled (it is force-enabled in Gitaly's test helpers and in GitLab Rails test/CI environments via `GITALY_TESTING_ENABLE_ALL_FEATURE_FLAGS=true`, and is intended for eventual production rollout per `doc/serialized_writes.md`). No special privilege, cost, or resource commitment is required beyond issuing an ordinary write RPC, making the DoS trivially and repeatedly triggerable by any user with push access to a shared repository.

### Recommendation
- Bound the total lock hold time independent of `Renew` calls (e.g., a hard maximum lease duration per transaction, after which the lock is force-released regardless of renewals), so a single transaction cannot indefinitely extend its exclusive hold.
- Consider allowing waiting transactions to detect starvation (e.g., FIFO fairness or aging-based priority) so a single repeatedly-renewed holder cannot starve all other writers.
- Emit alerting/metrics on excessively long `lockHoldDuration` per repository (already tracked) tied to automatic action (e.g., forced unlock) rather than purely observational on-call review.

### Proof of Concept
Given the documented flow in `doc/serialized_writes.md` ("Flow for one write"), a conceptual PoC:
1. Client A initiates a push to repo `R`; Gitaly casts a `Preparing` vote, and Praefect's `lockRepoForTransaction` acquires the exclusive write lock for `R` via `RepoReferenceWriteLockManager.Lock`.
2. Client A's push is engineered to be slow to complete Git's on-disk locking / write phase (e.g., pushing a very large number of refs, or exploiting slow storage) while its `Prepared` vote round-trip continues to call `Renew`, extending `expired_at` every ~20 seconds indefinitely.
3. Client B simultaneously attempts a normal push to the same repo `R`. Its `VoteTransaction(PREPARING_PHASE)` call blocks in `tryLock`, registered on `notificationCh`, and never proceeds until Client A's transaction reaches `COMMITTED_PHASE` — even though Client B's push is unrelated/non-conflicting in content and far more urgent.
4. As long as Client A keeps the transaction alive (renewing), Client B (and any other client) remains blocked with no mechanism to override, exactly mirroring the reported buyout DoS pattern of "once locked, nobody else can act until it ends." [7](#0-6) [8](#0-7)

### Citations

**File:** internal/praefect/transactions/manager.go (L272-334)
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

func (mgr *Manager) unlockRepoForTransaction(ctx context.Context, transactionID uint64, returnedErr error, phase gitalypb.VoteTransactionRequest_Phase) {
	if featureflag.PraefectSerializedWrite.IsDisabled(ctx) {
		return
	}

	// Decide whether this call releases the write lock. The lock spans all the
	// phases of one transaction and is only released when either the vote
	// failed (so callers can't advance to the next phase) or the committed
	// phase completes. Phases outside the reference transaction hook
	// lifecycle (e.g. SYNCHRONIZED_PHASE) never acquired a lock.
	var shouldUnlock bool
	switch phase {
	case gitalypb.VoteTransactionRequest_PREPARING_PHASE,
		gitalypb.VoteTransactionRequest_PREPARED_PHASE:
		shouldUnlock = returnedErr != nil
	case gitalypb.VoteTransactionRequest_COMMITTED_PHASE:
		shouldUnlock = true
	default:
		return
	}

	if !shouldUnlock {
		return
	}

	mgr.releaseRepoLockOnPhase(transactionID, phase.String())
}
```

**File:** internal/praefect/datastore/lock_manager.go (L257-278)
```go
// Lock acquires the write lock for a given virtualStorage, relativePath and txnID,
// blocking until the lock is acquired or ctx is cancelled.
func (r *RepoReferenceWriteLockManager) Lock(ctx context.Context, virtualStorage, relativePath string, txnID uint64,
) (RepoLock, error) {
	for {
		res := r.tryLock(ctx, virtualStorage, relativePath, txnID)
		if res.Err != nil {
			return RepoLock{}, res.Err
		}
		if res.Acquired {
			return RepoLock{Unlock: res.Unlock, Renew: res.Renew}, nil
		}

		select {
		case <-ctx.Done():
			res.Deregister()
			return RepoLock{}, fmt.Errorf("wait for lock: %w", ctx.Err())
		case <-res.NotificationCh:
			// wait for the lock to release
		}
	}
}
```

**File:** internal/praefect/datastore/lock_manager.go (L402-429)
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
```

**File:** doc/serialized_writes.md (L80-94)
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
```

**File:** doc/serialized_writes.md (L329-334)
```markdown
- **Serialization of concurrent writes to the same repository.** Writes
  that previously could run in parallel on different Gitaly nodes now
  run one at a time per repository. This is the entire point of the
  feature, but it means single-repo write throughput is upper-bounded
  by `1 / lock_hold_duration` rather than by the underlying Gitaly
  capacity.
```
