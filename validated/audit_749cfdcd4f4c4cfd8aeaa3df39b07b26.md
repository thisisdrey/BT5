### Title
Unfair lock re-acquisition in `RepoReferenceWriteLockManager` allows an attacker to indefinitely starve another user's push to the same repository - (File: `internal/praefect/datastore/lock_manager.go`)

### Summary
Praefect's `RepoReferenceWriteLockManager` serializes reference-transaction writes to a repository using a PostgreSQL row as a mutex. When the lock is released, **all** waiters registered on that lock ID are woken simultaneously and race to re-acquire it via an unordered compare-and-swap `UPSERT`. There is no FIFO ordering, ticket system, or fairness guarantee. An attacker with ordinary write access to a shared repository can continuously issue many cheap, fast-completing push/reference-update transactions that repeatedly win this unfair race against a legitimate, slower-arriving transaction, indefinitely delaying or effectively denying that victim's write until the victim's client gives up or its RPC deadline is exceeded.

### Finding Description
The lock is acquired per repository (`virtualStorage|relativePath`) at the `PREPARING_PHASE` of the `reference-transaction` hook, kept alive via `Renew` at `PREPARED_PHASE`, and released at `COMMITTED_PHASE` [1](#0-0) .

When a caller finds the lock held, it registers a channel on the shared `lockReleaseDispatcher` **before** attempting the `INSERT`, then blocks on it: [2](#0-1) 

On release, `Notified` wakes **every** registered waiter for that lock ID at once, with no notion of arrival order: [3](#0-2) 

All woken waiters then re-run the same unordered `tryLock` UPSERT concurrently:
```sql
INSERT INTO repository_reference_write_locks as locks (lock_id, holder_txn_id, expired_at)
VALUES ($1, $2, NOW() + $3::interval)
ON CONFLICT (lock_id) DO UPDATE
  SET holder_txn_id = EXCLUDED.holder_txn_id,
      expired_at    = EXCLUDED.expired_at
WHERE locks.expired_at < NOW() OR locks.holder_txn_id = $2
RETURNING lock_id, holder_txn_id, expired_at;
``` [4](#0-3) 

Whichever concurrent `INSERT ... ON CONFLICT` reaches PostgreSQL first wins — there is no queue position, priority, or backoff bias toward whoever has been waiting longest. An attacker who can push repeatedly to the repository controls both the *frequency* and the *size* of their own competing transactions: a push touching a single ref completes its full `PREPARING → PREPARED → COMMITTED` cycle (and releases the lock) far faster than a legitimate transaction that updates many refs, contends with a slow Gitaly node, or is simply queued behind other work. By continuously resubmitting minimal-cost transactions the instant the lock frees up, the attacker statistically wins nearly every re-acquisition race, so the victim's transaction is repeatedly notified, repeatedly loses the race, and is put back to sleep — the same "monitor state, cheaply re-race before the higher-value transaction can act" pattern as the referenced Tessera `rejectProposal` bug, just applied to a repository-scoped mutex instead of a collateral counter.

This is reachable purely through the ordinary write path (any RPC that mutates references on a repository the attacker can push to), requires no privileged role, no leaked token, no malicious peer/node, and no MITM — only unprivileged concurrent write access to a shared repository (e.g. two contributors with push access to the same project).

### Impact Explanation
A victim's legitimate reference update (branch push, merge, tag creation, etc.) to a contended repository can be starved indefinitely by a concurrently-pushing attacker, until the victim's gRPC call hits its context deadline and fails. This is a availability/DoS impact scoped to the `WriteLockManager`/serialized-writes RPC handling path (`internal/praefect/transactions/manager.go` phase handling, `internal/praefect/datastore/lock_manager.go`), degrading write availability for a specific targeted user/branch on a shared repository without requiring any elevated privilege. Because the feature is currently gated behind the `praefect_serialized_write` feature flag (default disabled) [5](#0-4) , impact is currently limited to deployments/environments that have enabled it, which somewhat limits severity today but will affect all clusters once the flag defaults on given the noted upstream Git dependency work.

### Likelihood Explanation
Likelihood is moderate: it requires the attacker to have push/write access to the *same* repository as the victim (a realistic scenario for shared projects with multiple contributors), and to race many small transactions against the victim's transaction. No cryptographic or timing precision is needed — the attacker simply needs to be a faster, more frequent writer than the victim, similar to how the original report's attacker needed to "defend" an underpriced proposal by repeatedly front-running. The `Notified`/thundering-herd design combined with the plain UPSERT CAS makes this systematically biased toward whichever caller can complete cheap transactions fastest, rather than a random or fair outcome.

### Recommendation
Introduce fairness into `RepoReferenceWriteLockManager`:
- Track waiter registration order (e.g., a FIFO queue keyed by `lockID`) and only grant the lock to the head of the queue on release, rather than waking every waiter to race an unordered `UPSERT`.
- Alternatively, use `SELECT ... FOR UPDATE SKIP LOCKED`-style sequencing or a ticket/sequence number stored alongside `holder_txn_id` so `tryLock` can prefer the lowest ticket number among contenders instead of "first `INSERT` to land."
- Consider bounding the number of times any single caller/IP/user identity can contend for the same `lock_id` within a time window, to prevent one client from monopolizing re-acquisition attempts.

### Proof of Concept
1. Attacker and victim both have push access to the same repository `R`.
2. Victim starts a push updating several refs in `R`; Gitaly reaches `PREPARING_PHASE` and calls `WriteLockManager.Lock`, finds the lock held by the attacker's in-flight transaction, registers on `lockReleaseDispatcher`, and blocks.
3. Attacker continuously performs rapid single-ref pushes to `R`. Each of the attacker's transactions: acquires the lock at `PREPARING_PHASE` → `renew`s once at `PREPARED_PHASE` → releases at `COMMITTED_PHASE` within milliseconds.
4. Each release triggers `Notified`, which wakes both the victim and the attacker's next queued transaction simultaneously; both re-run `tryLock`'s `UPSERT`. Because the attacker's transactions are minimal and immediately ready, they consistently win the race.
5. Repeat step 3 continuously. The victim's transaction is perpetually notified and re-loses the race, until the victim's gRPC client-side deadline is exceeded and the push fails with a context-deadline error, even though the attacker never needed elevated privileges — only ordinary concurrent push access to the same repository.

### Citations

**File:** doc/serialized_writes.md (L80-88)
```markdown
`internal/praefect/transactions/manager.go` owns the lock lifecycle. A
repository-scoped write lock backed by PostgreSQL is acquired, renewed, and
released according to the phase of each `VoteTransaction` call:

| Phase            | Action                                                  |
|------------------|---------------------------------------------------------|
| `PREPARING_PHASE`| `WriteLockManager.Lock` — block until exclusive access  |
| `PREPARED_PHASE` | `lock.Renew` — keep the lock alive across phases        |
| `COMMITTED_PHASE`| `lock.Unlock` — release once the write is durable       |
```

**File:** doc/serialized_writes.md (L347-352)
```markdown
## Enabling It

Serialized writes are gated by the
`praefect_serialized_write` feature flag. It defaults to **disabled** so
existing deployments are not affected.

```

**File:** internal/praefect/datastore/lock_manager.go (L101-112)
```go
// Notified signals all goroutines waiting on the released lock_id by closing their channels,
// then removes them from the waiters map.
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
