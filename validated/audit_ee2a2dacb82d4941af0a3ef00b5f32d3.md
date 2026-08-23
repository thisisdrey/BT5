### Title
Unfair, unbounded thundering-herd retry in Praefect's repository reference write-lock manager enables a flooding client to starve honest pushers of the same repository - (File: `internal/praefect/datastore/lock_manager.go`)

### Summary
Praefect's `RepoReferenceWriteLockManager` serializes concurrent pushes to the same repository behind a single PostgreSQL-backed lock so that the `reference-transaction` hook's `PREPARING_PHASE` vote can be globally ordered [1](#0-0) [2](#0-1) . When the lock is contended, every waiter is registered on a shared notification channel and, on release, *all* waiters are woken simultaneously with no queueing or fairness guarantee, then race each other with a fresh `tryLock` UPSERT to claim the lock [3](#0-2) [4](#0-3) . A client that can cheaply generate many concurrent, small write requests against the same repository (analogous to cheap dummy transactions filling BSC blocks) gains an outsized statistical advantage in every release-triggered race, letting it repeatedly re-acquire the lock and starve legitimate, single-request pushers indefinitely — mirroring the block-stuffing auction bug class where cheap flooding of a short, contended, first-to-act window lets an attacker force an outcome unfavorable to other participants.

### Finding Description
The lock acquisition/contention protocol is:

1. `tryLock` performs a conditional `INSERT ... ON CONFLICT DO UPDATE` UPSERT against `repository_reference_write_locks`, keyed by `lock_id = virtualStorage|relativePath` [5](#0-4) .
2. If the row is already held by a different, non-expired `txnID`, the caller registers on a notification channel for that `lock_id` and returns `Acquired=false` [6](#0-5) .
3. `Lock` loops: on failure to acquire, it blocks on the notification channel (or context cancellation) and retries `tryLock` when notified [4](#0-3) .
4. When the holder unlocks, the DB trigger fires `PG_NOTIFY`, and `lockReleaseDispatcher.Notified` closes **every** channel currently registered for that `lock_id`, waking all waiters at once [7](#0-6) .

There is no FIFO ordering, ticket system, or per-caller priority among waiters — every woken waiter (old and newly-registered) performs an equally-weighted retry of the same UPSERT, and whichever request's SQL statement reaches Postgres first wins. Because the acquisition attempt itself is cheap (a single UPSERT triggered during `PREPARING_PHASE`, i.e., *before* any actual ref locking or object transfer occurs [8](#0-7) ), a client can flood many concurrent transactions against the same `relativePath` far more cheaply than an honest client performs a real push. Each flood participant is another independent racer in every release-triggered scramble, so the probability that at least one of the attacker's requests wins each round approaches 1 as the number of concurrent flood requests grows, while a single honest waiter's chance of winning correspondingly shrinks toward 0. This is structurally the same primitive as the reported auction bug: a short, contended, first-to-act resolution window that can be dominated by whoever can cheaply flood more concurrent attempts into it.

### Impact Explanation
This allows a client with (or without, depending on the RPC/repo's auth model at Praefect) legitimate write access to a repository to indefinitely deny other clients the ability to push to that repository, because the shared per-repository write lock — a prerequisite for every write's `PREPARING_PHASE` to proceed — can be monopolized by whichever party sustains the highest concurrency of flood requests. This is a concrete availability impact (DoS of the write-serialization handler) for a specific repository, degrading Gitaly Cluster's core "every push eventually succeeds" guarantee, and is difficult for legitimate clients to detect or work around since they receive no distinguishing error — they simply keep losing every unlock race.

### Likelihood Explanation
The `PREPARING_PHASE` vote and lock acquisition happen unconditionally on every write RPC through Praefect for a repository with write serialization enabled [9](#0-8) , so the attack surface is reached by ordinary push traffic — no privileged access, malicious peer, or leaked token is required beyond whatever write access an attacker already has to trigger writes on the target repository. The only "skill" required is issuing many concurrent trivial write RPCs, which is materially cheaper than a normal push (which must additionally traverse ref-locking, object writing, and commit voting after winning the lock).

### Recommendation
Introduce fairness into the write-lock queue, e.g.:
- Maintain FIFO order for waiters per `lock_id` and only permit the head-of-line waiter to retry `tryLock` on release (ticket/turnstile pattern), instead of waking and racing all waiters simultaneously.
- Alternatively, bound the number of concurrent in-flight `PREPARING_PHASE` lock attempts per repository per client/connection, so a single source cannot flood the release race with many simultaneous racers.
- Consider adding jittered backoff plus a max-wait/queue-size limit analogous to the existing RPC concurrency limiter's `max_queue_wait`/`max_queue_size` semantics [10](#0-9) , so starved legitimate waiters fail fast with a retriable error rather than waiting indefinitely.

### Proof of Concept
1. Enable repository reference write-lock serialization for a virtual storage/repository (`RepoReferenceWriteLockManager`) [11](#0-10) .
2. Client H (honest) issues a single write RPC to `repo.git`, entering `Lock()` and, if contended, registering as one waiter.
3. Client A (attacker) concurrently issues N (e.g., 50-100) trivial write RPCs to the same `repo.git`, each entering `Lock()`/`tryLock()` and registering as additional waiters on the same `lock_id`.
4. When the current holder unlocks, `Notified` closes all registered waiter channels at once [7](#0-6) ; H and all of A's N racers retry `tryLock` simultaneously.
5. Statistically, one of A's N concurrent UPSERT attempts wins almost every round; H's single request is repeatedly out-raced.
6. A keeps re-issuing new cheap write attempts as soon as its own lock is released (or immediately after acquiring, releases and re-races), sustaining N concurrent racers indefinitely, so H's write to `repo.git` never completes.

### Citations

**File:** doc/serialized_writes.md (L43-55)
```markdown
### Git: the "preparing" phase

The serialization point is a new `reference-transaction` hook phase called
`preparing`, introduced upstream in:

> `60d8c1e97d62c27ef60db0bc3d5deadd6dfdb98d`
> *refs: add 'preparing' phase to the reference-transaction hook*
>
> First released in **Git v2.54.0**.

`preparing` fires **before** Git stages any ref update. This is the only phase
at which Praefect can take a global lock without already racing with the
on-disk locking machinery.
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

**File:** doc/serialized_writes.md (L116-124)
```markdown
### Flow for one write

1. Client calls a write RPC against Praefect.
1. Praefect forwards the RPC to each Gitaly node in the replica set.
1. Each Gitaly node, in `UpdateReference` (or the equivalent write path),
   casts a `Preparing` vote **before** locking refs locally. The hook RPC
   reaches Praefect, which calls `lockRepoForTransaction(PREPARING_PHASE)`:
   `WriteLockManager.Lock` blocks until the per-repository lock is free.
1. Both nodes return from `Preparing` together (quorum reached).
```

**File:** internal/praefect/datastore/lock_manager.go (L59-65)
```go
// RepoReferenceWriteLockManager manages per-repository reference write locks backed by
// PostgreSQL. It serializes write requests to the same repository by ensuring only one
// transaction holds the lock at a time. Callers use Lock to acquire the lock.
//
// PostgreSQL-backed locks are used instead of in-process mutexes to coordinate across
// multiple Praefect instances sharing the same database.
type RepoReferenceWriteLockManager struct {
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

**File:** internal/praefect/datastore/lock_manager.go (L165-203)
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

	return &RepoReferenceWriteLockManager{
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

**File:** internal/praefect/datastore/lock_manager.go (L356-371)
```go
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

**File:** doc/backpressure.md (L33-42)
```markdown
An in-memory queue of requests can build up in Gitaly that are waiting their turn. Because
this is a potential vector for a memory leak, two other values in the `[[concurrency]]`
configuration can prevent an unbounded in-memory queue of requests:

- `max_queue_wait` is the maximum amount of time a request can wait in the
  concurrency queue. When a request waits longer than this time, it returns
  an error to the client.
- `max_queue_size` is the maximum size the concurrency queue can grow for a
  given RPC. If a concurrency queue is at its maximum, subsequent requests
  return with an error. The queue size is per repository.
```
