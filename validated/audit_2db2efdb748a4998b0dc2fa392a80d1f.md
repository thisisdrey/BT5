### Title
Repository write-lock renewal bypasses expiry check, allowing indefinite lock extension after TTL - (File: `internal/praefect/datastore/lock_manager.go`)

### Summary
`RepoReferenceWriteLockManager.renew` extends a repository write lock's expiry by matching only on `lock_id` and `holder_txn_id`, without checking whether `expired_at` has already passed. This mirrors the reported bug class: an entity ("boost"/"lock") that was only valid for a bounded period can be extended past its expiry without the extension logic re-validating that the entity is still within its originally-granted validity window.

### Finding Description
The lock is acquired via `tryLock`, whose `UPSERT` explicitly gates acquisition-by-a-new-holder on expiry: [1](#0-0) 

But `renew`, called at `PREPARED_PHASE`/`COMMITTED_PHASE` of a reference transaction to keep the lease alive while Git holds its on-disk ref locks, only checks `lock_id` and `holder_txn_id` — it never checks `expired_at < NOW()`: [2](#0-1) 

This is invoked from `lockRepoForTransaction`, driven by ordinary client push RPCs going through Praefect's `VoteTransaction` phases: [3](#0-2) 

The lock's `renewInterval` (TTL) is hard-coded to 20 seconds: [4](#0-3) 

Because `renew` has no expiry guard, if the gap between a transaction's `PREPARING_PHASE` and its subsequent `PREPARED_PHASE`/`COMMITTED_PHASE` calls exceeds 20 seconds (e.g., due to a slow custom/reference-transaction hook, a deliberately slow client feeding pack data, or slow disk I/O on large ref updates), the lock row's `expired_at` will already be in the past. As long as no *other* transaction has raced in to steal the now-expired row via `tryLock`'s `WHERE ... expired_at < NOW()`, the original holder's `renew` call still succeeds unconditionally and pushes `expired_at` forward again — resetting the clock exactly like the reported `boost()` call reusing an invalidated NFT's magnitude without re-evaluating it.

### Impact Explanation
Repository writes through Praefect are explicitly serialized per repository via this lock to avoid cross-node ref-lock deadlocks (see `doc/serialized_writes.md`). The lock's TTL and the `WHERE expired_at < NOW()` clause in `tryLock` exist specifically so that a stalled/crashed holder cannot permanently block other legitimate writers to the same repository. `renew`'s missing expiry check silently defeats this safety valve: an ordinary user who can control the pacing of their own push (slow client, slow custom hook, or a repository they can trigger heavy ref/hook work on) can keep exceeding the TTL and still successfully "renew," extending their hold on the write lock indefinitely and starving out other pushers to the same repository. This is a availability/DoS impact scoped to a handler resource-limit gate (the write-lock gating mechanism), reachable purely from an unprivileged client's normal push flow, no leaked tokens or privileged actor required.

### Likelihood Explanation
The vulnerable window is a race: the exploit only "matters" if a contending transaction happens to attempt `tryLock` during the expired-but-not-yet-renewed gap. In low/medium contention this is unlikely to trigger real starvation on a single attempt, but a client that repeatedly and deliberately keeps its own PREPARING→PREPARED/COMMITTED gap above 20 seconds (trivial to do by controlling push payload pacing or custom hook execution time) can sustain the condition indefinitely for a targeted, frequently-pushed repository, making practical exploitation feasible with moderate effort and repo write access.

### Recommendation
Add the same expiry guard used in `tryLock` to the `renew` query, e.g. `WHERE locks.lock_id = $1 AND locks.holder_txn_id = $2 AND locks.expired_at >= NOW()`, and treat a no-rows result as "lock already expired/stolen," causing the renew call (and the in-flight transaction) to fail so a legitimately contending transaction can proceed.

### Proof of Concept
1. Attacker's client starts a push to `repo/target.git` through Praefect with `PraefectSerializedWrite` enabled; `lockRepoForTransaction` acquires the write lock at `PREPARING_PHASE` (`tryLock`).
2. Attacker deliberately stalls delivery of pack data / triggers a slow custom hook so that more than `renewInterval` (20s) elapses before the `PREPARED_PHASE` vote is cast.
3. During this window, the DB row's `expired_at` passes; a second, legitimate client attempting to push to the same repository calls `tryLock`, but arrives just before/after the attacker's renew — if no contender races in during the exact expired window, attacker's transaction calls `renew` (`internal/praefect/datastore/lock_manager.go:404-430`), which matches on `holder_txn_id` alone and succeeds, resetting `expired_at` to `NOW() + renewInterval`.
4. Attacker repeats steps 2–3 for each subsequent phase, indefinitely holding the write lock and starving other writers to the same repository, confirmed via the `renew extends lock and prevents acquisition by another txn` test path already in the test suite: [5](#0-4) .

### Citations

**File:** internal/praefect/datastore/lock_manager.go (L188-189)
```go
	// Start a scheduled cleanup background job to remove expired locks.
	lockRenewInterval := 20 * time.Second
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

**File:** internal/praefect/datastore/lock_manager_test.go (L177-196)
```go
	t.Run("renew extends lock and prevents acquisition by another txn", func(t *testing.T) {
		t.Parallel()
		mgr := NewRepoReferenceWriteLockManager(ctx, db, dbConfig, logger)
		waitForListener(mgr.handler.ready)

		res1 := mgr.tryLock(ctx, "default", "repo/renew.git", 1)
		require.NoError(t, res1.Err)
		require.NoError(t, res1.Renew(ctx))
		defer func() {
			require.NoError(t, res1.Unlock())
		}()

		// After renewal the lock is still held; txn2 must not acquire it.
		res2 := mgr.tryLock(ctx, "default", "repo/renew.git", 2)
		require.NoError(t, res2.Err)
		require.False(t, res2.Acquired)
		require.NotNil(t, res2.NotificationCh)
		require.NotNil(t, res2.Deregister)
		defer res2.Deregister()
	})
```
