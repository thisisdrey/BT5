### Title
Repository write lock is not released when a reference-transaction aborts, causing bounded DoS on subsequent legitimate writes - ([File: internal/gitaly/hook/referencetransaction.go])

### Summary
Praefect's serialized-writes feature holds a per-repository PostgreSQL write lock from the `preparing` phase of a reference-transaction until either a vote failure or the `committed` phase is reached. Gitaly's reference-transaction hook handler, however, silently drops the `aborted` state without casting any vote to Praefect, so an aborted transaction never signals lock release. The lock then survives until its fixed TTL expires, blocking all other legitimate writes to that repository for the interim.

### Finding Description
`GitLabHookManager.ReferenceTransactionHook` maps hook states to voting phases, but only handles `Preparing`, `Prepared`, and `Committed` explicitly; any other state (including `ReferenceTransactionAborted`) falls into the `default: return nil` branch and never calls `voteOnTransaction`: [1](#0-0) 

On the Praefect side, `lockRepoForTransaction` acquires a PostgreSQL-backed repository write lock on `PREPARING_PHASE` and renews it on `PREPARED_PHASE`/`COMMITTED_PHASE`: [2](#0-1) 

The lock is only released in `unlockRepoForTransaction` when a `PREPARING_PHASE`/`PREPARED_PHASE` vote *fails*, or when `COMMITTED_PHASE` is reached: [3](#0-2) 

Because Gitaly never emits a vote for the `aborted` reference-transaction state, Praefect never learns that the transaction ended, and neither the error-path nor the committed-path unlock branch fires. The lock is only reclaimed by the periodic cleanup job or TTL expiry, both hard-coded to a 20-second `renewInterval` (cleanup runs every `2 * renewInterval`): [4](#0-3) [5](#0-4) 

This mirrors the reported bug class: an unrelated/expected state transition (here, an explicit release signal on abort) is assumed to happen but does not, leaving a resource (the repository write lock) in a state that blocks otherwise-valid operations (subsequent pushes) until an external timeout elapses — analogous to `BondNFT` transfers reverting because a `distribute` call never occurs.

### Impact Explanation
While serialized writes are enabled (`PraefectSerializedWrite`), any reference-transaction that reaches the `preparing` phase (lock acquired) and then aborts — for example due to a concurrent conflicting ref update, a compare-and-swap failure in `git-update-ref`, or any other git-level abort after quarantine/hook processing but before commit — leaves the per-repository lock held for up to ~40 seconds (TTL + cleanup interval) with no legitimate holder. During that window, all other write RPCs (`Push`, `OperationService` writes, etc.) to the same repository from any user are serialized behind a lock nobody will proactively release, effectively delaying/DoS-ing concurrent legitimate writes to that repository.

### Likelihood Explanation
Reference-transaction aborts can be triggered by ordinary users through normal, unprivileged git operations — most notably concurrent pushes to overlapping references, which is a common and expected occurrence in active repositories (not a privileged or malicious-peer scenario). A user (or script) could also deliberately race pushes against the same ref to repeatedly abort transactions and keep the repo's write lock churn high, extending the effective blocking window.

### Recommendation
Ensure Gitaly casts an explicit vote/signal to Praefect (or calls the equivalent of `stopTransaction`) when the reference-transaction hook receives the `aborted` state, so `unlockRepoForTransaction` (or an equivalent release path) is invoked immediately rather than relying on TTL expiry. At minimum, extend the `switch` in `ReferenceTransactionHook` to handle `ReferenceTransactionAborted` by voting on `voting.Aborted` (or invoking `m.stopTransaction`), and add a corresponding case in `lockRepoForTransaction`/`unlockRepoForTransaction` for the aborted phase.

### Proof of Concept
1. Enable `PraefectSerializedWrite`.
2. Start a write RPC on repository `R` that reaches the `preparing` reference-transaction phase (lock acquired in `repository_reference_write_locks`).
3. Cause the transaction to abort before committing — e.g., have a second concurrent push update the same ref so that `git-update-ref`'s compare-and-swap fails, or otherwise force a `git` transaction abort.
4. Observe that `ReferenceTransactionHook` receives state `ABORTED`, hits the `default: return nil` branch in `internal/gitaly/hook/referencetransaction.go`, and no `VoteTransaction` call for this phase reaches Praefect.
5. Issue a subsequent legitimate write RPC to repository `R` immediately; observe it blocks in `WriteLockManager.Lock` until the stale lock's `expired_at` passes and `cleanUpExpiredRepoRefWriteLocks` (or a fresh `tryLock` expiry check) reclaims it, up to ~40 seconds later — confirming the lock was never proactively released on abort.

### Citations

**File:** internal/gitaly/hook/referencetransaction.go (L53-106)
```go
	var phase voting.Phase
	switch state {
	// We're voting in preparing state to tell Praefect that the reference changes are ready,
	// although they are not locked yet. At this stage, we can serialize write transactions by
	// acquiring a lock for the transaction and releasing it only when the transaction is
	// committed or aborted. Other transactions must wait for the lock.
	case ReferenceTransactionPreparing:
		phase = voting.Preparing
	// We're voting in prepared state to tell Praefect we've locked the reference changes and
	// queued them for the transaction. We can abort the transaction if needed.
	case ReferenceTransactionPrepared:
		phase = voting.Prepared

		if tx != nil {
			updates, err := parseChanges(objectHash, bytes.NewReader(changes))
			if err != nil {
				return fmt.Errorf("parse changes: %w", err)
			}

			initialValues := map[git.ReferenceName]git.Reference{}
			for reference, update := range updates {
				if update.OldOID != "" {
					initialValues[reference] = git.NewReference(reference, update.OldOID)
				} else {
					initialValues[reference] = git.NewSymbolicReference(reference, update.OldTarget)
				}
			}

			// Only record the initial values of the reference in the prepare step as this
			// change hasn't yet been committed.
			if err := tx.RecordInitialReferenceValues(ctx, initialValues); err != nil {
				return fmt.Errorf("record initial reference value: %w", err)
			}
		}
	// We're also voting in committed state to tell Praefect we've actually persisted the
	// changes. This is necessary as some RPCs fail return errors in the response body rather
	// than as an error code. Praefect can't tell if these RPCs have failed. Voting on committed
	// ensure Praefect sees either a missing vote or that the RPC did commit the changes.
	case ReferenceTransactionCommitted:
		phase = voting.Committed

		if tx != nil {
			updates, err := parseChanges(objectHash, bytes.NewReader(changes))
			if err != nil {
				return fmt.Errorf("parse changes: %w", err)
			}

			if err := tx.UpdateReferences(ctx, updates); err != nil {
				return fmt.Errorf("update references: %w", err)
			}
		}
	default:
		return nil
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

**File:** internal/praefect/datastore/lock_manager.go (L188-201)
```go
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
