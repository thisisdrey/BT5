### Title
Reference write-lock TTL can expire mid-transaction, allowing lock theft while on-disk ref locks are still held, reintroducing the cross-node deadlock the serialization feature exists to prevent - (File: `internal/praefect/transactions/manager.go`, `internal/praefect/datastore/lock_manager.go`)

### Summary
The LayerZero report's root cause is that an untrusted, user-supplied timing/gas parameter can violate an assumption a non-blocking protocol depends on, and the protocol has no mechanism to detect or recover from the resulting inconsistent state short of manual intervention. Gitaly's Praefect "serialized writes" feature has a structurally analogous timing assumption: it grants a repository-wide write lock with a fixed 20-second TTL and renews it exactly once per reference-transaction hook phase, with no periodic keep-alive while a phase is in progress. An ordinary user's push whose git-side phase duration exceeds this hard-coded window can cause the lock to expire and be handed to a second, unrelated push while the first push's Gitaly nodes still hold on-disk ref locks — exactly the multi-node deadlock scenario this feature was built to eliminate.

### Finding Description
`doc/serialized_writes.md` explains that the whole reason this feature exists is to prevent a cross-node ref-lock deadlock: two concurrent transactions must never be allowed to hold on-disk ref locks for overlapping refs on different nodes at the same time. [1](#0-0) 

The exclusivity guarantee is implemented purely via a PostgreSQL row with a TTL (`expired_at`), where the TTL length (`renewInterval`) is hard-coded to 20 seconds and is *not* derived from, or aware of, how long the actual git operation (ref locking, hook execution, object writing) will take: [2](#0-1) 

The lock is acquired once at `PREPARING_PHASE` and is only renewed when the git reference-transaction hook later fires `PREPARED_PHASE` or `COMMITTED_PHASE` — i.e. renewal happens exactly once per phase transition, not periodically while a phase is still in progress: [3](#0-2) 

Per the doc, git's `prepared` phase fires only *after* the on-disk ref locks have already been taken on each participating node: [4](#0-3) 

This means the vulnerable window is precisely between `PREPARING_PHASE` (lock acquired, no on-disk locks yet) and `PREPARED_PHASE` (on-disk locks already taken, first renewal). If that window — driven entirely by how long the client's push takes to reach the point where git takes ref locks (e.g. a push touching a very large number of refs, a slow custom hook, or simply a large/slow transfer) — exceeds 20 seconds, the PostgreSQL lock row expires. The `tryLock` UPSERT explicitly allows a *different* transaction to take over an expired row: [5](#0-4) 

so a second, completely unrelated push racing for the same repository can now acquire the "exclusive" lock and instruct its own Gitaly nodes to begin taking on-disk ref locks — while the first (slow) push's nodes may still be holding their own on-disk locks, having not yet reached `PREPARED_PHASE`. This is exactly the T1/T2 overlapping-ref, cross-node deadlock scenario the feature document says must never happen.

When the first (slow) transaction eventually calls `PREPARED_PHASE`, `renew()` will find no row for its `txnID` (it was stolen) and return an error: [6](#0-5) 

`lockRepoForTransaction` propagates this error, and `unlockRepoForTransaction` treats a failed `PREPARED_PHASE` as a signal to release "its" lock — but `unlock()` is a conditional `DELETE ... WHERE holder_txn_id = $2`, so this is a no-op against the second transaction's now-current row. There is no mechanism analogous to "force-resume" that detects this inconsistent state, alerts on it, or safely aborts one of the two racing on-disk ref-lock holders; the two Gitaly nodes are simply left to deadlock or corrupt state independently, and the operator has no built-in signal beyond generic ref-lock/hook failures to diagnose it.

### Impact Explanation
This defeats the entire purpose of the serialized-writes feature: it can reintroduce the cross-node ref-lock deadlock the document explicitly says this design exists to prevent, for repositories under Praefect with `PraefectSerializedWrite` enabled. Depending on how the underlying Gitaly nodes behave when their ref locks contend with a second transaction's locks (block, timeout, or error), the result is either a stalled write path for that repository (requiring operator/administrator intervention to identify and clear stuck git/hook processes and stale on-disk `.lock` files) or inconsistent ref state across replicas. Because the trigger only requires an ordinary git push whose reference-transaction phases are naturally slow (large number of refs, slow custom hooks, large payload), no special privileges are required.

### Likelihood Explanation
The 20-second TTL is fixed regardless of repository size, ref count, or hook complexity, and is renewed only once per phase transition rather than continuously for the duration of the phase. Any push that legitimately or intentionally takes longer than 20 seconds between the `preparing` and `prepared` git hook phases (e.g., many refs in one push, custom hooks with I/O, disk/CPU contention) is sufficient to trigger the race — this does not require adversarial timing manipulation beyond simply making one push "slow enough," making it plausible even without a deliberate attacker, and trivially reproducible/amplifiable by one.

### Recommendation
- Make the lock's effective TTL track the actual duration of git's locking work rather than a fixed constant, e.g. by renewing periodically (heartbeat) for the entire duration a phase is being processed, not just once at each phase transition.
- When `renew()` fails because the row is gone/stolen, treat this as a serialization-integrity violation rather than an ordinary transaction failure: log/alert loudly, and where possible have the losing transaction actively abort/release its on-disk locks before a second transaction can proceed, instead of silently no-op'ing the `unlock`.
- Consider detecting and rejecting (or splitting into sub-transactions) pushes whose expected `preparing`→`prepared` duration is likely to exceed the renew interval by a wide margin (e.g., extremely large ref counts in a single push), similar to enforcing sane bounds on user-influenced parameters as recommended for the LayerZero gas-parameter issue.

### Proof of Concept
1. Enable `PraefectSerializedWrite` against a test Praefect cluster with the PostgreSQL-backed `RepoReferenceWriteLockManager`.
2. Start transaction T1 on repository `R`: call `VoteTransaction` with `PREPARING_PHASE` (lock acquired, `expired_at = now+20s`).
3. Simulate a slow git-side locking phase for T1 by delaying more than 20 seconds before calling `PREPARED_PHASE` (mirrors `internal/praefect/transaction_write_serialization_test.go`'s `TestPraefectWriteSerialization_HangAfterPrepared`, which already demonstrates back-dating `expired_at` to simulate exactly this expiry window).
4. While T1's lock row is expired, start transaction T2 on the same repository `R` and call `VoteTransaction` with `PREPARING_PHASE` — the UPSERT in `tryLock` succeeds because `locks.expired_at < NOW()`, and T2 is granted the "exclusive" lock even though T1's Gitaly nodes have not released their on-disk ref locks.
5. T1 subsequently calls `PREPARED_PHASE`; `renew()` finds no matching row for T1's `txnID` and returns an error, which is swallowed as a no-op `unlock` against T2's row — T1 and T2 are now both able to drive their respective Gitaly nodes' on-disk ref locking concurrently for the same repository, recreating the cross-node deadlock scenario documented as the reason this feature exists.

### Citations

**File:** doc/serialized_writes.md (L9-17)
```markdown
## Why Serialize Writes

The voting protocol relies on Git's `reference-transaction` hook to vote on a
ref update at each phase of the transaction. The phases Git historically
exposed are:

- `prepared` — references have been locked on disk
- `committed` — updates have been persisted
- `aborted` — the transaction was rolled back
```

**File:** doc/serialized_writes.md (L19-36)
```markdown
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

**File:** internal/praefect/transactions/manager.go (L279-306)
```go
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
