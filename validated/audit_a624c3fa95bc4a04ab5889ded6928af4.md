### Title
Race between `StopTransaction`/`cancelTransaction` and a concurrent `VoteTransaction` call bypasses the Praefect write-lock guard, reintroducing the cross-node ref-lock deadlock the serialization feature is meant to prevent - ([File: internal/praefect/transactions/manager.go])

### Summary
`internal/praefect/transactions/manager.go` implements a per-repository write-lock "guard" (`PraefectSerializedWrite`) that is supposed to span the entire lifecycle of one reference-transaction (from `PREPARING_PHASE` through `COMMITTED_PHASE`). Similar to the HSG bug, where `checkTransaction()`/`checkAfterExecution()` used an unsynchronized increment/decrement pair (`_guardEntries`) around the reentrant `execTransaction()` call, Gitaly's guard here is a `sync.Map` entry (`mgr.repoLocks[transactionID]`) that is stored on `PREPARING_PHASE` and deleted/finalized independently of the transaction's terminal-state flag, without holding a single lock across both operations.

### Finding Description
The lock lifecycle is:

- `lockRepoForTransaction` (`internal/praefect/transactions/manager.go:272-306`) stores a `datastore.RepoLock` in `mgr.repoLocks` on `PREPARING_PHASE`, and on later phases loads it via `mgr.repoLocks.Load(transactionID)`. If the load misses, the code **silently treats this as the "git build without preparing phase" compatibility case and skips all locking** [1](#0-0) .
- `StopTransaction` (`internal/praefect/transactions/manager.go:228-251`) and `cancelTransaction` (`internal/praefect/transactions/manager.go:122-152`) each release the repo lock (`releaseRepoLock`/`releaseRepoLockOnPhase`, which does `LoadAndDelete` + `Unlock()` against Postgres) **before** calling `transaction.stop()` / `transaction.cancel()`, and this release happens while holding `mgr.lock` only briefly — `mgr.lock.Unlock()` occurs right after the lock removal, prior to the `transaction.stop()`/`cancel()` call that actually marks the transaction as terminated [2](#0-1) [3](#0-2) .
- Meanwhile, `voteTransaction` (`internal/praefect/transactions/manager.go:154-177`) only holds `mgr.lock` for the map lookup (`transaction, ok := mgr.transactions[transactionID]`) and releases it immediately, before calling `lockRepoForTransaction` and `transaction.vote(...)` outside of any shared lock [4](#0-3) .

This creates a window where a concurrent `VoteTransaction(PREPARED_PHASE/COMMITTED_PHASE)` call for the same `transactionID` can:
1. Successfully find `transaction, ok := mgr.transactions[transactionID]` (still `ok=true`, because `StopTransaction` never removes the entry from `mgr.transactions`, only `cancelTransaction` does).
2. Call `lockRepoForTransaction`, which finds `mgr.repoLocks.Load(transactionID)` already missing (removed by the concurrent `StopTransaction`/`cancelTransaction`), and falls into the "no preparing phase recorded" tolerance branch that **skips lock/renew entirely** [1](#0-0) .
3. Proceed to call `transaction.vote(...)`, potentially before `transaction.stop()`/`transaction.cancel()` has actually flipped the transaction's internal state to a terminal/rejecting one.

At this point the repository's exclusive write lock in Postgres has already been released (`Unlock()` deleted the `repository_reference_write_locks` row and fired the `NOTIFY`), so another, unrelated write transaction for the *same repository* can immediately acquire the freed lock and begin taking on-disk ref locks — while the just-"stopped" transaction's vote/commit logic is still concurrently executing for the same repo without any lock, exactly reproducing the two-transactions-taking-overlapping-ref-locks deadlock scenario the entire `PraefectSerializedWrite` feature (`doc/serialized_writes.md`) was built to eliminate [5](#0-4) .

Note the doc even explicitly states the fallback branch is intended only for "no `PREPARING_PHASE` was recorded for the transaction" due to old Git binaries [6](#0-5) ; the code cannot actually distinguish that legitimate case from the lock having been prematurely removed by a racing `StopTransaction`/`cancelTransaction` call, because both manifest identically as `repoLocks.Load` returning "not found".

### Impact Explanation
This defeats write serialization for a repository exactly as the pre-existing cross-node ref-lock deadlock issue described in `doc/serialized_writes.md` (`Why Serialize Writes`), which can result in the cluster deadlocking (both Gitaly-node ref locks held, each waiting for the other, neither releasing until quorum, which never arrives) — an availability/DoS condition on that repository's future writes across the whole Praefect-managed cluster. It can also, depending on Git's own on-disk lock handling under contention, permit interleaved reference updates from two "concurrently serialized" transactions, which is precisely the corruption/split-brain scenario the feature exists to prevent.

### Likelihood Explanation
`StopTransaction` is documented as being triggered by ordinary, user-reachable conditions: "if the primary node fails the RPC call in code that is only executed on the primary" (e.g. a pre-receive hook rejecting a push on the primary node after secondaries have already begun voting) [7](#0-6) . An attacker/ordinary user who can craft a push that is likely to be rejected mid-flight by server-side policy (protected branch, custom pre-receive hook, etc.) while other Gitaly nodes are still casting `Prepared`/`Committed` votes for the same transaction can reliably trigger the race window described above. The feature is currently gated behind the `praefect_serialized_write` feature flag, defaulting to disabled [8](#0-7) , so exploitability today depends on the flag being enabled, but the race exists unconditionally in the code path whenever the feature is on.

### Recommendation
Serialize the "release lock" and "mark transaction terminal" operations under the same lock/critical section that `voteTransaction` also participates in — e.g., hold `mgr.lock` (or a per-transaction mutex) across both `releaseRepoLock(...)` and `transaction.stop()`/`transaction.cancel()` in `StopTransaction`/`cancelTransaction`, and have `voteTransaction` check the transaction's terminal state under that same lock before calling `lockRepoForTransaction`/`transaction.vote(...)`. Additionally, distinguish "lock never existed because of pre-2.54 Git" from "lock existed but was already released by Stop/Cancel" (e.g., via an explicit per-transaction flag set at `RegisterTransaction`/`StopTransaction` time) so the fallback-skip branch in `lockRepoForTransaction` cannot be entered as a side effect of this race.

### Proof of Concept
1. Enable `PraefectSerializedWrite`.
2. Client pushes to a repo replicated across Gitaly nodes N1 and N2 via Praefect; a reference-transaction with `transactionID = T` is registered.
3. N1 casts `PREPARING_PHASE` vote for T → Praefect's `lockRepoForTransaction` acquires the Postgres write lock and stores it in `mgr.repoLocks[T]`.
4. Before N2 casts its `PREPARED_PHASE`/`COMMITTED_PHASE` vote for T, a custom `pre-receive` hook on the primary rejects the push, causing Praefect's graceful-stop path to call `StopTransaction(T)`. Inside `StopTransaction`, `releaseRepoLock(T)` runs (`LoadAndDelete` + Postgres `DELETE`/`NOTIFY`), releasing the lock and unblocking any repo-T writer waiting on `tryLock`.
5. Concurrently, before `transaction.stop()` finishes executing, N2's `VoteTransaction(T, PREPARED_PHASE)` call reaches `voteTransaction`, finds `mgr.transactions[T]` still present, calls `lockRepoForTransaction`, hits the "no preparing recorded" fallback (because the lock was just deleted in step 4) and skips locking, then calls `transaction.vote(...)`.
6. Meanwhile a second, unrelated write to the same repository (transaction `T2`) that was waiting on the Postgres lock is woken by the `NOTIFY` from step 4 and acquires the lock, proceeding to take on-disk ref locks on N1/N2 at the same time T's `PREPARED_PHASE` continues to execute for the same repository — recreating the two-transactions-holding-overlapping-ref-locks deadlock scenario documented in `doc/serialized_writes.md`.

This requires precise timing to reproduce deterministically, which limits it to a manual review / logical demonstration; I was unable to execute this scenario in a live cluster and cannot fully confirm the exact interleaving is achievable without instrumentation of `mgr.lock`/`repoLocks` timing (e.g., via test hooks), which is outside what static code reading can verify with certainty.

### Citations

**File:** internal/praefect/transactions/manager.go (L122-130)
```go
func (mgr *Manager) cancelTransaction(ctx context.Context, transaction *transaction) error {
	mgr.lock.Lock()
	defer mgr.lock.Unlock()

	delete(mgr.transactions, transaction.ID())
	// Release while holding mgr.lock so a concurrent voteTransaction can't
	// re-acquire the repo lock between delete and cancel.
	mgr.releaseRepoLock(transaction.ID())
	transaction.cancel()
```

**File:** internal/praefect/transactions/manager.go (L154-177)
```go
func (mgr *Manager) voteTransaction(ctx context.Context, transactionID uint64, storageName, repoRelativePath, node string,
	phase gitalypb.VoteTransactionRequest_Phase, vote voting.Vote,
) (returnedErr error) {
	mgr.lock.Lock()
	transaction, ok := mgr.transactions[transactionID]
	mgr.lock.Unlock()

	if !ok {
		return fmt.Errorf("%w: %d", ErrNotFound, transactionID)
	}

	err := mgr.lockRepoForTransaction(ctx, transactionID, storageName, repoRelativePath, phase)
	if err != nil {
		return fmt.Errorf("lock transaction %d: %w", transactionID, err)
	}
	defer func() {
		mgr.unlockRepoForTransaction(ctx, transactionID, returnedErr, phase)
	}()
	if err := transaction.vote(ctx, node, vote); err != nil {
		return err
	}

	return nil
}
```

**File:** internal/praefect/transactions/manager.go (L228-251)
```go
func (mgr *Manager) StopTransaction(ctx context.Context, transactionID uint64) error {
	mgr.lock.Lock()
	transaction, ok := mgr.transactions[transactionID]
	if ok {
		// Release while holding mgr.lock so a concurrent voteTransaction
		// can't acquire a fresh repo lock between this release and stop.
		mgr.releaseRepoLock(transactionID)
	}
	mgr.lock.Unlock()

	if !ok {
		return fmt.Errorf("%w: %d", ErrNotFound, transactionID)
	}
	if err := transaction.stop(); err != nil {
		return err
	}

	mgr.logger.WithFields(log.Fields{
		"transaction.id": transactionID,
	}).DebugContext(ctx, "VoteTransaction: transaction stopped")
	mgr.counterMetric.WithLabelValues("stopped").Inc()

	return nil
}
```

**File:** internal/praefect/transactions/manager.go (L287-299)
```go
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
```

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

**File:** doc/serialized_writes.md (L349-352)
```markdown
Serialized writes are gated by the
`praefect_serialized_write` feature flag. It defaults to **disabled** so
existing deployments are not affected.

```

**File:** doc/serialized_writes.md (L379-388)
```markdown
On a bundled Git that contains `60d8c1e9…`, Git emits the `preparing` phase
for these paths too and the serialization is complete.

On a bundled Git **without** that commit, Git only emits `prepared` and
`committed`. Praefect tolerates this by skipping the lock check when no
`PREPARING_PHASE` was recorded for the transaction. The transaction completes
normally, but **serialization is dormant for that write**. The deadlock
described above remains possible on those paths until `GIT_VERSION_PREV` is
bumped past `60d8c1e9…`. Once it is, the tolerance branch becomes unreachable
and can be removed alongside a startup-time minimum Git version check.
```

**File:** doc/hooks.md (L194-196)
```markdown
To fix the hang, transactions support graceful stops: if the primary node fails
the RPC call in code that is only executed on the primary, then it will stop the
transaction and thus tell other Gitaly nodes to stop waiting for quorum.
```
