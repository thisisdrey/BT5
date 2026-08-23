### Title
Ordinary write RPCs can strand the Praefect repository-wide serialization lock, causing a repeatable denial-of-service against all other writers of that repository - (File: `internal/praefect/transactions/manager.go`)

### Summary
Praefect's new write-serialization feature (`praefect_serialized_write`) grants exclusive, repository-scoped write access to whichever transaction wins the `PREPARING_PHASE` race, and only releases that lock when the same transaction later reaches `COMMITTED_PHASE`, fails explicitly, or is stopped/canceled. An ordinary user who can push/write to a repository can acquire this lock and then simply never advance the transaction to `COMMITTED_PHASE` (by aborting/killing their own connection instead of cleanly canceling it, or by never sending the closing stream data), stranding the lock for the full 20-second TTL. Because the lock is a single global per-repository resource, every other concurrent writer to the same repository — regardless of privilege — is blocked for that window. Repeating the attack lets a single unprivileged writer indefinitely deny writes to a shared repository, mirroring the referenced bug class where an attacker deliberately lets its own operation fail after reserving a one-time exclusive resource, starving the honest actor.

### Finding Description
`Manager.lockRepoForTransaction` acquires a repository-wide lock at `PREPARING_PHASE` via `WriteLockManager.Lock`, which "blocks until exclusive access" is granted [1](#0-0) . The lock is only released on an explicit error at `PREPARING`/`PREPARED`, or unconditionally at `COMMITTED_PHASE`: [2](#0-1) 

If the transaction is never explicitly canceled/stopped and never reaches `COMMITTED_PHASE` — e.g. because the client that owns the transaction disconnects abruptly (process killed, network partition) rather than cleanly closing the gRPC call so `cancelTransaction`'s deferred `releaseRepoLock` never runs — the lock row simply sits until its TTL expires: [3](#0-2) 

The TTL is hard-coded to 20 seconds and is only refreshed by an explicit `Renew` call at `PREPARED_PHASE`; if the attacker's own transaction never reaches that phase, the lock is not renewed and simply expires after the initial 20-second grant, but during that entire window every other write to the same repository is queued behind it because the lock key is `virtualStorage|relativePath` — i.e. scoped to the whole repository, not to individual refs [4](#0-3) . The doc itself acknowledges the crash scenario is expected and only bounded by "another transaction can take the row over once it passes" the TTL [5](#0-4) , but it does not bound how many times, or how quickly in succession, an ordinary writer can re-trigger this stranding behavior.

Because these phases are driven transparently by ordinary Git operations (`git push`, or Gitaly write RPCs such as `UserCommitFiles`, `UserCreateBranch`, `UpdateReferences`) through the reference-transaction hook, no privileged Gitaly-node or Praefect-internal access is required — any user who has push/write permission to a repository can trigger `PREPARING_PHASE` and then simply let their own connection die before `COMMITTED_PHASE`, exactly analogous to the referenced report's attacker letting `callInstantWithdraw` intentionally run out of gas after having already reserved the `userInteractionNumber`: the attacker's own operation is allowed to fail, but the side effect (the reserved exclusive resource) persists and blocks everyone else.

### Impact Explanation
Every other concurrent write to the targeted repository (from any user, including privileged maintainers or CI) is queued and delayed for up to the full lock TTL (20 seconds) per occurrence [6](#0-5) . Since an ordinary writer can repeat the abandon-before-commit pattern indefinitely and cheaply against any repository they can push to, they can sustain an unbounded denial-of-service against all writers of that repository, which is a legitimate availability impact on the Praefect write-serialization RPC handler.

### Likelihood Explanation
The feature is gated behind the `praefect_serialized_write` feature flag, which currently defaults to disabled [7](#0-6) , so likelihood in an unmodified default deployment is low today. However, once the flag is enabled (the doc frames this as the intended eventual state, with rollout tracked in a work item), the attack requires nothing beyond ordinary push/write permission to one repository, and the abandon-the-connection technique is trivial to script and repeat, making exploitation straightforward for any authenticated collaborator with write access.

### Recommendation
- Bound the total time a `PREPARING`/`PREPARED` transaction may hold the repository lock before an explicit `COMMITTED_PHASE` or cancellation, independent of the 20s TTL renewal path, and actively evict/alert on repeated abandonment from the same actor/repository.
- Attach the lock's lifetime to the underlying client RPC context/deadline so a dropped connection is detected and unlocked promptly rather than relying solely on TTL expiry.
- Add rate limiting or backoff per repository/user for `PREPARING_PHASE` acquisitions that are not followed by `COMMITTED_PHASE` within a short grace period, to prevent a single writer from repeatedly starving others.

### Proof of Concept
1. Enable `praefect_serialized_write` for a virtual storage.
2. As an ordinary user with push access to repository R, start a write operation that triggers the reference-transaction hook (e.g. `git push` or `UserCommitFiles`) so that Praefect calls `VoteTransaction` with `PREPARING_PHASE`, acquiring the lock in `RepoReferenceWriteLockManager.tryLock` [3](#0-2) .
3. Before the `PREPARED`/`COMMITTED` phases fire, forcibly kill the client process/connection (e.g., `kill -9` the git process or drop the network connection) instead of letting it complete or explicitly cancel.
4. Observe that a second, unrelated user's write to repository R blocks/queues for up to 20 seconds (`renewInterval`) while the abandoned lock row remains valid, as described in the phase-release table [8](#0-7) .
5. Repeat steps 2-3 back-to-back to keep the repository perpetually locked out for other writers.

### Citations

**File:** doc/serialized_writes.md (L84-94)
```markdown
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

**File:** doc/serialized_writes.md (L170-177)
```markdown
- `lock_id` is formatted as `virtualStorage|relativePath`
  (see `repoLockID()` in `lock_manager.go`). One row per repository.
- `holder_txn_id` is the Praefect transaction ID currently owning the lock.
- `expired_at` is the lock's deadline. If a holder crashes or never explicitly
  unlocks, another transaction can take the row over once it passes.
- The index on `expired_at` supports the background sweep below.
- The trigger fires `PG_NOTIFY` on every `DELETE` so the listener in any
  Praefect instance can wake any waiter for that `lock_id`.
```

**File:** doc/serialized_writes.md (L181-183)
```markdown
The lock TTL (`renewInterval`) is **20 seconds**, hard-coded in
`NewRepoReferenceWriteLockManager`. Every successful acquire or renew pushes
`expired_at` forward by 20 seconds.
```

**File:** doc/serialized_writes.md (L349-351)
```markdown
Serialized writes are gated by the
`praefect_serialized_write` feature flag. It defaults to **disabled** so
existing deployments are not affected.
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
