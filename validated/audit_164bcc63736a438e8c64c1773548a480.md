### Title
Unbounded repository write-lock renewal via repeated `PREPARED_PHASE` votes enables permanent write-lock DoS - (File: internal/praefect/transactions/manager.go)

### Summary
Praefect's serialized-write feature backs each repository write lock with a database row whose `expired_at` deadline is unconditionally pushed forward by a fixed `renewInterval` (20s) every time a `PREPARED_PHASE` (or `COMMITTED_PHASE`) vote is processed for the owning transaction, with no cap on the number of renewals or on total lock-hold time [1](#0-0) . The lock is released only when the vote fails or the transaction reaches `COMMITTED_PHASE` [2](#0-1) . This mirrors the reported ERC20 pattern: a cheap, attacker-controllable action (`transferFrom(…, 0)` there; a repeated `PREPARED_PHASE` vote here) unconditionally resets a completion/expiry timer, indefinitely blocking the legitimate terminal step (burning shares there; acquiring the write lock for other pushers here).

### Finding Description
The reference-transaction hook drives `VoteTransaction` phases (`PREPARING_PHASE` → `PREPARED_PHASE` → `COMMITTED_PHASE`) from an ordinary git push handled by the client's own Gitaly node [3](#0-2) . On the Praefect side, `lockRepoForTransaction` acquires the per-repository `WriteLockManager` lock at `PREPARING_PHASE`, and unconditionally calls `lock.Renew(ctx)` on every subsequent `PREPARED_PHASE`/`COMMITTED_PHASE` vote for that transaction ID [4](#0-3) . The `renew` SQL statement always sets `expired_at = NOW() + renewInterval` regardless of how many times it has already been called or how long the lock has been held [5](#0-4) . Unlock only happens for `PREPARING_PHASE`/`PREPARED_PHASE` when the vote *fails*, or unconditionally at `COMMITTED_PHASE` [2](#0-1) . There is no maximum lock-hold duration, no limit on the number of renewals, and no requirement that renewals monotonically approach completion — exactly the missing-bound flaw described in the Sherlock report, where `_afterTokenTransfer` resets `poolSharesPreparedTimestamp` on every transfer with no limiting condition.

An unprivileged client driving its own push can keep the reference-transaction hook — and therefore the transaction — parked in the `PREPARED_PHASE` state (e.g., by slow-rolling the push so the underlying git ref-transaction stalls between "prepared" and "committed", or by having the internal vote RPC retried) for as long as it wishes, and each such vote resets the lock's 20-second expiry. Because the lock is repository-scoped (keyed by `virtualStorage|relativePath`) and blocks acquisition by any other transaction ID until it expires or is explicitly released [6](#0-5) , this indefinitely blocks all other writers to that repository.

### Impact Explanation
As long as the attacker keeps renewing, no other client can acquire the write lock for the same repository, so all other pushes/writes to that repository are denied — a persistent, repository-wide write DoS caused entirely by an unprivileged pusher's own transaction never reaching `COMMITTED_PHASE`. This is comparable in severity to the original report's "funds locked forever," here manifesting as "writes blocked forever" for legitimate users of a shared repository.

### Likelihood Explanation
Exploitability depends on the attacker's ability to keep their own reference-transaction hook invocation in the `PREPARED_PHASE` state for extended, repeated windows (e.g., via a deliberately slow or stalling push) without failing the vote or advancing to commit. The `PraefectSerializedWrite` feature flag must be enabled for the lock path to run at all [7](#0-6) . Given this dependency on precise control of the git ref-transaction timing/retry behavior, likelihood is moderate rather than trivially guaranteed, but the underlying code has no structural safeguard (max hold time, renewal counter, or monotonic-progress check) preventing it.

### Recommendation
Bound the total lock lifetime independent of renewal count, e.g., track the original acquisition time and refuse to renew (forcing unlock/failure) past a maximum hold duration; alternatively, cap the number of `PREPARED_PHASE` renewals per transaction and fail the transaction (releasing the lock) if it is exceeded, similar to skipping a no-op reset when there is no genuine progress in the underlying report's suggested fix.

### Proof of Concept
Not executed; based on static analysis of `lockRepoForTransaction`/`renew` unconditionally extending `expired_at` on every `PREPARED_PHASE` vote with no upper bound, combined with the reference-transaction hook being driven by an ordinary user's own push [8](#0-7) .

### Citations

**File:** internal/praefect/transactions/manager.go (L275-277)
```go
	if featureflag.PraefectSerializedWrite.IsDisabled(ctx) {
		return nil
	}
```

**File:** internal/praefect/transactions/manager.go (L279-304)
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
```

**File:** internal/praefect/transactions/manager.go (L308-334)
```go
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

**File:** internal/gitaly/service/hook/reference_transaction.go (L19-53)
```go
func (s *server) ReferenceTransactionHook(stream gitalypb.HookService_ReferenceTransactionHookServer) error {
	request, err := stream.Recv()
	if err != nil {
		return structerr.NewInternal("receiving first request: %w", err)
	}

	if err := validateReferenceTransactionHookRequest(stream.Context(), s.locator, request); err != nil {
		return structerr.NewInvalidArgument("%w", err)
	}

	var state hook.ReferenceTransactionState
	switch request.GetState() {
	case gitalypb.ReferenceTransactionHookRequest_PREPARING:
		state = hook.ReferenceTransactionPreparing
	case gitalypb.ReferenceTransactionHookRequest_PREPARED:
		state = hook.ReferenceTransactionPrepared
	case gitalypb.ReferenceTransactionHookRequest_COMMITTED:
		state = hook.ReferenceTransactionCommitted
	case gitalypb.ReferenceTransactionHookRequest_ABORTED:
		state = hook.ReferenceTransactionAborted
	default:
		return structerr.NewInvalidArgument("invalid hook state")
	}

	stdin := streamio.NewReader(func() ([]byte, error) {
		req, err := stream.Recv()
		return req.GetStdin(), err
	})

	if err := s.manager.ReferenceTransactionHook(
		stream.Context(),
		state,
		request.GetEnvironmentVariables(),
		stdin,
	); err != nil {
```

**File:** internal/praefect/datastore/lock_manager.go (L304-371)
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
	if err := rows.Err(); err != nil {
		deregister()
		return tryLockResult{
			Acquired: false,
			Err:      fmt.Errorf("acquire repo reference write lock: %s, %w", lockID, err),
		}
	}

	r.lockAcquiredTotal.WithLabelValues(virtualStorage, "contended").Inc()
	return tryLockResult{
		Acquired:       false,
		NotificationCh: notificationCh,
		Deregister:     deregister,
	}
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
