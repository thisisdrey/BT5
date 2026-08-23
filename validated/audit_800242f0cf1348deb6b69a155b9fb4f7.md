### Title
Repository write-lock TTL doesn't account for unbounded hook/primary-logic delay between vote phases, allowing lock expiry and write-lock bypass mid-transaction - (File: `internal/praefect/transactions/manager.go`, `internal/praefect/datastore/lock_manager.go`)

### Summary
Praefect's serialized-writes feature protects against a reference-transaction ref-lock deadlock by having Praefect grant one repository-scoped PostgreSQL lock per repository and holding it across the `PREPARING` → `PREPARED` → `COMMITTED` phases of a Gitaly reference-transaction hook [1](#0-0) . The lock's expiry (`expired_at`) is only pushed forward by a fixed `renewInterval` of 20 seconds each time `lockRepoForTransaction` is called at the `PREPARED_PHASE` or `COMMITTED_PHASE` boundary [2](#0-1) [3](#0-2) . However, the actual wall-clock time an ordinary push can spend *between* phases is explicitly documented elsewhere in the same codebase as unbounded, because it depends on "primary-only logic" (access checks that scale with repository size, and custom hooks) — this is exactly why the separate `transactionTimeout` for voting RPCs is set to 5 minutes instead of a tight bound [4](#0-3) . The 20-second lock TTL was never adjusted to account for this same acknowledged extension mechanism.

### Finding Description
`RepoReferenceWriteLockManager.tryLock` acquires the lock via an UPSERT whose `WHERE` clause explicitly allows any transaction to "steal" a lock row whose `expired_at` has passed, regardless of whether the original holder is still alive and mid-flight: `WHERE locks.expired_at < NOW() OR locks.holder_txn_id = $2` [5](#0-4) . The lock is renewed by exactly one call to `renew()` per phase transition, each time extending `expired_at` by only 20 seconds [6](#0-5) .

The renewal cadence assumes each inter-phase gap completes within 20 seconds. But the intended design explicitly anticipates the opposite: the sibling `transaction.Vote` RPC uses a 5-minute timeout precisely because "the primary-only logic's execution time scales with repository size for the access checks and... is potentially even unbounded due to custom hooks" [4](#0-3) . Custom `pre-receive`/`update`/`post-receive` hooks and access-check logic are attacker/user-influenceable — an ordinary user can configure or trigger slow custom hooks, or push to a very large repository, both of which are legitimate, reachable ways to stretch the gap between the `PREPARING` and `PREPARED` (or `PREPARED` and `COMMITTED`) reference-transaction hook phases well past 20 seconds, exactly as the Astaria bug's auction could legitimately extend past the assumed `auctionWindow` via repeated bid-triggered extensions that the accounting check failed to add in.

If that gap exceeds 20 seconds without an intervening renew, the PostgreSQL lock row expires. A second, concurrent push to the same repository can then successfully "steal" the lock via the same UPSERT path while the first transaction is still active and has already had quorum on `Preparing` (meaning Git on the Gitaly nodes may already be about to, or already have, taken on-disk ref locks). This defeats the entire purpose of the serialization feature described in `doc/serialized_writes.md`, which exists specifically to prevent two concurrent transactions from taking conflicting on-disk ref locks in different orders across nodes, i.e., it reopens the very cross-node ref-lock deadlock/race window that serialized writes were built to close [7](#0-6) .

### Impact Explanation
This is a gating-bypass of the write-serialization mechanism guarding hook/ref-lock ordering across a Praefect-managed replicated repository. Once the lock is prematurely stolen, two writes to the same repository can proceed concurrently through the on-disk ref-locking phase on different Gitaly nodes, reintroducing the documented deadlock condition (T1 locks ref A on node 1 and ref B on node 2 while T2 does the reverse) or, more subtly, letting an "expired-lock" transaction and a "new" transaction interleave writes to the same repository's refs, causing lost updates/reference corruption. Both are exactly the class of state-consistency failure the underlying report described (state/accounting decided under a stale duration assumption becomes wrong because the process was legitimately extended).

### Likelihood Explanation
Reaching this requires only an ordinary push against a repository that has custom hooks or that is large enough that primary-node access-check logic and hook execution take more than 20 seconds between reference-transaction hook phases — both are supported, common, user-controllable configurations (custom server hooks, repository size), not privileged-actor or malicious-peer scenarios. No implementation detail is guessed: the code that renews at fixed 20s intervals, and the code/comment acknowledging the same delay source is unbounded, both live directly in this repository.

### Recommendation
Either (a) renew the write lock periodically on a background ticker for the entire duration a transaction holds it (mirroring how the Astaria fix widened the window by the maximum possible extension), rather than only once per phase boundary, or (b) size `renewInterval`/lock TTL to be at least as large as the maximum time a phase can legitimately take (e.g., align it with or exceed `transactionTimeout` in `internal/gitaly/transaction/manager.go`), and treat an unexpectedly failed renew as a hard transaction abort rather than silently allowing takeover.

### Proof of Concept
1. Configure a repository with a custom `pre-receive` (or `update`/`post-receive`) hook that sleeps for >20 seconds, or push to a sufficiently large repository so that the access-check phase exceeds 20 seconds.
2. Client A pushes to the repository. Praefect's `Manager.voteTransaction` acquires the write lock at `PREPARING_PHASE` via `lockRepoForTransaction` [8](#0-7) ; quorum for `Preparing` is reached and Gitaly nodes proceed toward locking refs on disk and running the slow hook before casting `Prepared`.
3. Because more than 20 seconds elapse before the `PREPARED_PHASE` renew call, the `expired_at` row in `repository_reference_write_locks` passes.
4. Client B concurrently pushes to the same repository; its `tryLock` UPSERT succeeds because `locks.expired_at < NOW()` is now true, per the same query used and tested in `lock_manager_test.go`'s "txn2 can steal an expired lock" case [9](#0-8) .
5. Both A's and B's writes can now proceed through on-disk ref locking concurrently, defeating serialization and reproducing the deadlock/race the feature is documented to prevent.

### Citations

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

**File:** doc/serialized_writes.md (L78-99)
```markdown
## How It Works

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

**File:** internal/praefect/datastore/lock_manager.go (L165-201)
```go
// NewRepoReferenceWriteLockManager creates a new RepoReferenceWriteLockManager. It starts
// a background listener for lock release notifications and a background job to
// clean up expired locks. Both run until ctx is cancelled.
func NewRepoReferenceWriteLockManager(ctx context.Context, qc glsql.Querier, conf config.DB, logger log.Logger) *RepoReferenceWriteLockManager {
	resilientListenerTicker := helper.NewTimerTicker(5 * time.Second)

	lockReleasingListener := NewResilientListener(conf, resilientListenerTicker, logger, lockReleasingListenerReconnectsTotal)
	handler := &lockReleaseDispatcher{
		waiters: make(map[LockID][]chan struct{}),
		ready:   make(chan struct{}),
	}

	// In production, we start the listener asynchronously to speed up startup.
	// Conceptually, we could block in the constructor until the connection is established,
	// since the manager cannot function correctly without the listener. However, this has
	// a downside: if the database is temporarily slow during startup, the constructor may hang.
	go func() {
		err := lockReleasingListener.Listen(ctx, handler, RepositoryReferenceWriteLockReleasesChannel)
		if err != nil && !errors.Is(err, context.Canceled) {
			logger.WithError(err).Error("notifications listener terminated")
		}
	}()

	// Start a scheduled cleanup background job to remove expired locks.
	lockRenewInterval := 20 * time.Second
	cleanUpJobTicker := helper.NewTimerTicker(2 * lockRenewInterval)
	go func() {
		defer cleanUpJobTicker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-cleanUpJobTicker.C():
				cleanUpExpiredRepoRefWriteLocks(ctx, qc, logger)
			}
		}
	}()
```

**File:** internal/praefect/datastore/lock_manager.go (L304-320)
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

**File:** internal/gitaly/transaction/manager.go (L19-31)
```go
const (
	// transactionTimeout is the timeout used for all transactional
	// actions like voting and stopping of transactions. This timeout is
	// quite high: usually, a transaction should finish in at most a few
	// milliseconds. There are cases though where it may take a lot longer,
	// like when executing logic on the primary node only: the primary's
	// vote will be delayed until that logic finishes while secondaries are
	// waiting for the primary to cast its vote on the transaction. Given
	// that the primary-only logic's execution time scales with repository
	// size for the access checks and that it is potentially even unbounded
	// due to custom hooks, we thus use a high timeout. It shouldn't
	// normally be hit, but if it is hit then it indicates a real problem.
	transactionTimeout = 5 * time.Minute
```

**File:** internal/praefect/datastore/lock_manager_test.go (L157-175)
```go
	t.Run("txn2 can steal an expired lock", func(t *testing.T) {
		t.Parallel()
		mgr := NewRepoReferenceWriteLockManager(ctx, db, dbConfig, logger)
		waitForListener(mgr.handler.ready)

		_, err1 := mgr.Lock(ctx, "default", "repo/expire.git", 1)
		require.NoError(t, err1)

		// Simulate expiry by back-dating the lock directly in the DB.
		_, err := db.ExecContext(ctx, `
			UPDATE repository_reference_write_locks
			SET expired_at = NOW() - INTERVAL '1 second'
			WHERE lock_id = 'default|repo/expire.git'`)
		require.NoError(t, err)

		lock2, err2 := mgr.Lock(ctx, "default", "repo/expire.git", 2)
		require.NoError(t, err2)
		require.NoError(t, lock2.Unlock())
	})
```
