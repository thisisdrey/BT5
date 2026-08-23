## Title
Repository reference-write lock can be monopolized by rapid self re-acquisition, permanently starving legitimate pushers of the same repository - (File: `internal/praefect/datastore/lock_manager.go`)

### Summary
Praefect's new serialized-writes feature introduces a single, repository-scoped write lock (`repository_reference_write_locks`) that every push to a repository must acquire at the `PREPARING_PHASE` of the Git `reference-transaction` hook before proceeding, and hold through `PREPARED`/`COMMITTED`. This is directly analogous to the Alchemix `AlchemistV2` mint-limit griefing bug: a shared, bounded resource (there: the per-block mint headroom; here: the sole write-lock slot for a repository) can be re-claimed by the same cheap, self-controlled action (mint→burn there; lock→commit→relock here) faster than legitimate contenders can, permanently starving everyone else from a resource that the protocol assumes will be fairly shared.

### Finding Description
The lock lifecycle lives in `internal/praefect/datastore/lock_manager.go` and `internal/praefect/transactions/manager.go`:

- `tryLock` performs an UPSERT that grants the lock to whichever caller's query lands first when the row is free/expired [1](#0-0) .
- When the lock is contended, the manager registers the waiter and blocks on a notification channel that is fired for **all** current waiters simultaneously when the lock is released [2](#0-1) , causing every waiter to race the same UPSERT again (`Lock` loops back into `tryLock` on notification) [3](#0-2) .
- There is no fairness/queueing guarantee (no FIFO ticket, no per-waiter priority) — it is a pure "first UPSERT wins" race among all currently-woken waiters.
- `voteTransaction` acquires the lock at `PREPARING_PHASE` and only releases it on failure or at `COMMITTED_PHASE` [4](#0-3) .

A single automated client pushing trivial, fast commits (e.g., empty commits or 1-byte changes) to a shared/high-traffic repository can complete the acquire→prepare→commit→release cycle in milliseconds and immediately re-enter the race for the same lock row, while ordinary human-driven `git push` clients incur normal network/negotiation latency between phases. Because the thundering-herd wakeup gives no preference to the longest-waiting caller, the fast attacker script will statistically win the re-acquisition race on almost every cycle, permanently starving all other users' legitimate pushes to that repository — exactly the "repeatedly mint then burn to keep the shared limit pegged" pattern from the source report, translated to "repeatedly lock then commit-and-relock to keep the shared write-lock pegged."

### Impact Explanation
Because the lock is mandatory for every `git push` to a given repository once `PraefectSerializedWrite` is enabled (Git ≥ 2.54 bundled), an attacker who can push to a shared/public repository (an ordinary authenticated user, not a privileged actor) can deny all other legitimate contributors the ability to push to that repository indefinitely, without needing elevated permissions, without corrupting any data, and while consuming only trivial compute/network resources of their own. This is a genuine DoS of the write path for a repository, comparable in kind (though narrower in blast radius, being per-repository rather than protocol-wide) to the referenced medium-severity griefing finding.

### Likelihood Explanation
The attack requires only ordinary push access to the target repository and a simple automated script; no race-window timing beyond normal HTTP/SSH push overhead is needed, and the thundering-herd re-notify design makes the outcome probabilistically favor the fastest repeat caller. This is a realistic, low-cost griefing vector for any attacker (or misbehaving CI job) with write access to a busy shared repository.

### Recommendation
- Enforce fairness in lock acquisition, e.g., a FIFO waiting queue per `lock_id` (ticket/sequence number) instead of "wake everyone, race the UPSERT again."
- Add a minimum inter-acquisition cooldown or per-holder rate limit so the same `txnID`/actor cannot immediately re-win the lock for the same repository without yielding to other waiters.
- Consider bounding maximum consecutive lock holds per identity/IP within a time window and metrics/alerting on abnormally high acquisition churn for a single `lock_id`.

### Proof of Concept
1. Enable `PraefectSerializedWrite` and Git ≥ 2.54 support for the `preparing` reference-transaction phase.
2. From an account with push access to a shared repository, run a tight loop pushing trivial empty commits (`git commit --allow-empty && git push`).
3. Have a second, normal user attempt a push to the same repository at the same time.
4. Observe that the attacker's fast lock→commit→release cycle repeatedly wins the `tryLock` UPSERT race in `internal/praefect/datastore/lock_manager.go` (lines 304-371) against the legitimate user's slower push, causing the legitimate push to time out waiting on `NotificationCh` in `Lock` (lines 259-278) indefinitely as long as the attacker keeps looping.

### Citations

**File:** internal/praefect/datastore/lock_manager.go (L103-112)
```go
func (d *lockReleaseDispatcher) Notified(n glsql.Notification) {
	lockID := LockID(n.Payload)
	d.mu.Lock()
	chs := d.waiters[lockID]
	delete(d.waiters, lockID)
	d.mu.Unlock()
	for _, ch := range chs {
		close(ch) // wake all waiters
	}
}
```

**File:** internal/praefect/datastore/lock_manager.go (L259-278)
```go
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
