### Title
Fixed 20-second DB lock TTL can expire before Gitaly's on-disk ref-lock phase completes, letting a second transaction "steal" the write lock and reintroduce the cross-node ref-lock deadlock the serialization feature exists to prevent - (File: `internal/praefect/datastore/lock_manager.go`, `internal/praefect/transactions/manager.go`)

### Summary
Praefect's write-lock serialization uses two independent timing mechanisms to guard the same critical section: a PostgreSQL-backed lock with a fixed 20-second TTL (`renewInterval`), and Git's own on-disk ref-locking process, whose duration is controlled by separate, unrelated knobs (`core.packedRefsTimeout` = 10s, `core.filesRefLockTimeout` = 1s, actual disk I/O, and cross-node quorum waiting). When the real elapsed time between the `PREPARING_PHASE` (DB lock acquired) and the next `Renew` call at `PREPARED_PHASE` exceeds 20 seconds, the DB lock silently expires and can be "stolen" by a different transaction while the original transaction may still be in the middle of taking Git's on-disk ref lock. This desynchronizes the two locks in the same way as the referenced CVG cycle-vs-veCVG-week bug: one clock (Postgres `NOW()`-based TTL) advances independently of the other (Git's process-local on-disk lock duration), and the entity meant to be treated as a single lock ends up in two different states simultaneously.

### Finding Description
The design documented in `doc/serialized_writes.md` explicitly states the purpose of this mechanism: serialize transactions *before* any node takes an on-disk ref lock, specifically to prevent a cross-node ref-lock deadlock. The phase table is: [1](#0-0) 

The lock TTL is a fixed, hard-coded interval, not tied to how long the on-disk locking phase actually takes: [2](#0-1) 

The acquisition query lets any different transaction "steal" the row once `expired_at < NOW()`, with no coordination with the actual on-disk lock state on any Gitaly node: [3](#0-2) 

Critically, per the documented flow, the Gitaly node takes the **on-disk** ref lock *before* it casts the `Prepared` vote (which is the only point that renews the DB lock): [4](#0-3) 

`lockRepoForTransaction` only renews the DB lock's TTL on `PREPARED_PHASE`/`COMMITTED_PHASE`, i.e. only after the on-disk lock has already been acquired: [5](#0-4) 

If the interval between `PREPARING_PHASE` and the `Prepared` vote (which includes cross-node quorum waiting via `transaction.vote` and the actual on-disk ref-lock acquisition, itself bounded by `core.packedRefsTimeout`/`core.filesRefLockTimeout`) exceeds the fixed 20-second TTL, the DB row expires. A second, waiting transaction can then acquire the "exclusive" write lock via the UPSERT's expiry guard and proceed to take on-disk locks on its own replica set — while the first transaction's Gitaly node(s) may still be holding on-disk locks from the earlier, now-orphaned transaction. This is exactly the multi-node interleaving the `preparing` phase and PostgreSQL lock were introduced to prevent, as described in the "Why Serialize Writes" rationale referenced in the doc.

The renewal cadence (`2 * renewInterval` for the sweep, 20s TTL, discrete renewal only at phase boundaries) is an **absolute, server-clock-driven** measurement, decoupled from the **variable, workload-driven** duration of the actual git-level critical section it is meant to protect — the same class of dual-clock mismatch as the CVG report (cycle length driven by `block.timestamp`/discrete rollovers vs. veCVG's fixed weekly rounding).

### Impact Explanation
When the two clocks desynchronize, the safety invariant "only one transaction can be inside the on-disk locking phase for a given repository at a time" is violated. This can:
- Reintroduce the exact cross-node ref-lock deadlock scenario the entire `PREPARING_PHASE` mechanism was built to eliminate (per `doc/serialized_writes.md`'s "Why Serialize Writes" motivation), causing a stuck/hung write RPC across the replica set (DoS of the write RPC handler for that repository).
- Cause the original (stale) transaction to have its later `Renew` calls fail once another transaction has stolen the row (`renew` errors when the row's `holder_txn_id` no longer matches), aborting an otherwise-valid write after it already mutated on-disk state — leading to inconsistent replica states or failed/dropped writes that must be retried, i.e. an availability/consistency impact on ordinary push/write RPCs.
This is reachable by any ordinary user whose push or write RPC (e.g., a large `DeleteRefs`/branch mutation that must rewrite `packed-refs`, or one that simply experiences transient node/disk slowness or quorum delay) takes longer than the hard-coded 20-second window between `PREPARING_PHASE` and the `Prepared` vote.

### Likelihood Explanation
Likelihood is moderate: it requires the on-disk locking/quorum phase to exceed 20 seconds, which is plausible on loaded systems, large repositories with many references (packed-refs rewrites), slow storage/NFS, or when quorum votes from multiple Gitaly replicas are delayed — conditions already acknowledged elsewhere in the codebase as real (`core.packedRefsTimeout` was raised to 10s specifically because "in practice... this is not sufficient" for high-activity repos, per `internal/git/gitcmd/command_factory.go`). No privileged access, malicious peer, or leaked credential is needed — an ordinary heavy write workload from a legitimate user is sufficient to trigger the race.

### Recommendation
Do not rely on a fixed wall-clock TTL renewed only at discrete phase boundaries to bound a variable-duration critical section. Options: (1) renew the DB lock immediately upon successfully casting the `Preparing` vote, before Git begins the on-disk locking work, and repeatedly while waiting on quorum, rather than only after `Prepared`; (2) make the TTL a function of the actual timeout budget of the underlying git-lock operation (`core.packedRefsTimeout` / `core.filesRefLockTimeout` / request deadline) so it can never expire before the operation it is protecting could plausibly complete; (3) have the "stolen" detection propagate synchronously to the original holder (e.g., via the NOTIFY channel) so it aborts its on-disk lock acquisition immediately instead of only discovering the loss at the next `Renew` call.

### Proof of Concept
1. Configure Praefect with `PraefectSerializedWrite` enabled and a repository with many references (large `packed-refs`) on constrained/slow storage.
2. Transaction A casts a `PREPARING_PHASE` vote, acquiring the DB write lock (20s TTL) per `lock_manager.go`'s `tryLock`.
3. Delay transaction A's Gitaly node in the on-disk locking step (e.g. via slow disk/large `packed-refs` rewrite, or by making `core.packedRefsTimeout` ~10s and stacking multiple contended ref locks) so that more than 20 seconds elapse before it casts its `Prepared` vote.
4. Meanwhile, transaction B, targeting the same repository, calls `PREPARING_PHASE`; once A's `expired_at` passes `NOW()`, B's UPSERT succeeds per the expiry guard (`internal/praefect/datastore/lock_manager.go` lines 313-320), and B proceeds to take on-disk locks on its own replica set while A's node may still hold on-disk locks from the stale transaction.
5. Observe A's subsequent `Renew` call (at `PREPARED_PHASE`) fail because `holder_txn_id` no longer matches, aborting A's transaction after on-disk mutation may have already begun — demonstrating the desynchronization between the Postgres-clock lock and the Git-level lock duration.

### Citations

**File:** doc/serialized_writes.md (L84-89)
```markdown
| Phase            | Action                                                  |
|------------------|---------------------------------------------------------|
| `PREPARING_PHASE`| `WriteLockManager.Lock` — block until exclusive access  |
| `PREPARED_PHASE` | `lock.Renew` — keep the lock alive across phases        |
| `COMMITTED_PHASE`| `lock.Unlock` — release once the write is durable       |

```

**File:** doc/serialized_writes.md (L116-128)
```markdown
### Flow for one write

1. Client calls a write RPC against Praefect.
1. Praefect forwards the RPC to each Gitaly node in the replica set.
1. Each Gitaly node, in `UpdateReference` (or the equivalent write path),
   casts a `Preparing` vote **before** locking refs locally. The hook RPC
   reaches Praefect, which calls `lockRepoForTransaction(PREPARING_PHASE)`:
   `WriteLockManager.Lock` blocks until the per-repository lock is free.
1. Both nodes return from `Preparing` together (quorum reached).
1. Each Gitaly node locks refs on disk and votes `Prepared`. Praefect renews
   the lock.
1. Each Gitaly node writes the update and votes `Committed`. Praefect releases
   the lock.
```

**File:** doc/serialized_writes.md (L181-183)
```markdown
The lock TTL (`renewInterval`) is **20 seconds**, hard-coded in
`NewRepoReferenceWriteLockManager`. Every successful acquire or renew pushes
`expired_at` forward by 20 seconds.
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

**File:** internal/praefect/transactions/manager.go (L272-304)
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
```
