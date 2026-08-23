### Title
Client-controlled `phase` value on `VoteTransaction` lets any write-path skip the Praefect repository write-lock, defeating serialized-write deadlock protection - (File: internal/praefect/transactions/manager.go)

### Summary
`lockRepoForTransaction`/`unlockRepoForTransaction` in Praefect's transaction manager treat the `phase` field of `VoteTransactionRequest` as the sole, trusted "stage" that decides whether a repository write-lock is acquired, renewed, or released. There is no verification that a `PREPARING_PHASE` vote actually preceded a `PREPARED_PHASE`/`COMMITTED_PHASE` vote for the same transaction; if it didn't, the code silently treats the write as unlocked/"tolerated" rather than rejecting or otherwise reconciling the mismatch. This mirrors the `GMToken.stage` bug class: a state field that can be advanced/reported in any order, with other logic relying on it as if it reflected the actual protocol state, and with a fallback that quietly disables the real protection instead of enforcing consistency.

### Finding Description
The write-serialization design documented in `doc/serialized_writes.md` requires exactly this ordering per transaction: `PREPARING_PHASE` → (lock) → `PREPARED_PHASE` → (renew) → `COMMITTED_PHASE` → (unlock). The enforcement code is: [1](#0-0) 

When a `PREPARED_PHASE` or `COMMITTED_PHASE` vote arrives for a `transactionID` that never recorded a `PREPARING_PHASE` lock, the code does not error, does not retroactively acquire the lock, and does not fail the transaction — it just logs a debug message and returns `nil`, allowing the transaction to proceed as if it were exclusive: [2](#0-1) 

The `phase` value itself is nothing more than an enum sent by the calling Gitaly node in the RPC request and stored/consulted with no cross-check against the transaction's real internal state: [3](#0-2) [4](#0-3) 

The documentation itself acknowledges the fallback is a deliberate but dangerous compromise: on Git binaries or write paths that don't emit `preparing` (many first-party write paths are listed: `FetchSourceBranch`, `Replicate`, `ResolveConflicts`, `UserCommitFiles`, `UserRevert`, `RebaseToRef`, `MergeToRef`, and anything using `localrepo.UpdateRef`/`localrepo.FetchInternal`), Praefect "tolerates" the missing phase and lets the transaction complete without ever taking the lock — i.e., serialization is silently dormant for that specific write while every other write believes serialization is active.

Because the `phase` value is attacker-adjacent (it is derived from ordinary Git operations and write RPCs triggered by ordinary pushes/fetches on paths the docs enumerate as unprepared), an ordinary user simply exercising one of those un-migrated write paths concurrently with another write to the same repository can reproduce exactly the cross-node ref-lock deadlock/race that this whole mechanism (`internal/praefect/datastore/lock_manager.go`, `internal/praefect/transactions/manager.go`) exists to prevent — the referenced root-cause issue gitlab-org/gitaly#7059.

### Impact Explanation
When the fallback branch is taken, the per-repository write-serialization guarantee is silently bypassed for that transaction while concurrent transactions on the same repository still believe the guarantee holds. This reintroduces the exact deadlock/race condition described in `doc/serialized_writes.md` ("Why Serialize Writes"): concurrent transactions can take on-disk ref locks on different Gitaly nodes in inconsistent order, deadlocking the cluster or leaving refs updated inconsistently across replicas. Because several first-class, unauthenticated-by-anything-other-than-normal-push write paths (`FetchSourceBranch`, `Replicate`, `ResolveConflicts`, `UserCommitFiles`, `UserRevert`, `RebaseToRef`, `MergeToRef`, `localrepo.UpdateRef`/`FetchInternal`) are explicitly documented as not emitting `PREPARING_PHASE`, this is not a narrow edge case — it's the default behavior for an entire class of RPCs, which is a denial-of-service / cluster-consistency risk against a Gitaly Cluster (Praefect) deployment reachable from ordinary repository operations.

### Likelihood Explanation
High for environments running the `praefect_serialized_write` feature flag with a bundled Git that doesn't yet contain `60d8c1e9…`, or when using any of the still-unmigrated write RPCs listed in the "Limitations" section of `doc/serialized_writes.md` — no privileged access or malicious peer is required; ordinary concurrent pushes/fetches/merges/rebases to the same repository trigger it. The condition is entirely a function of which write path is used and Git version, not attacker sophistication.

### Recommendation
Do not silently "tolerate" a missing `PREPARING_PHASE`. Instead:
1. Track, per transaction, whether the write path is expected to emit `PREPARING_PHASE` (based on RPC/write-path identity, not solely on the phase value reported at vote time), and fail closed (reject or force serialization by an alternate mechanism) when a `PREPARED_PHASE`/`COMMITTED_PHASE` vote arrives without a corresponding lock.
2. Alternatively, require all Praefect write paths to route through a phase-independent locking gate keyed on the RPC/handler itself, rather than relying purely on the client-reported `phase` enum, removing the redundant trust placed in `phase` as an implicit stage/state variable.
3. Add a startup-time Git version check (already anticipated in the docs) that refuses to enable `praefect_serialized_write` unless the bundled Git guarantees `PREPARING_PHASE` emission on every the write path, closing the tolerance branch entirely rather than leaving it reachable in production.

### Proof of Concept
1. Deploy Praefect with `praefect_serialized_write` enabled and a Git version that does not include `60d8c1e97d62c27ef60db0bc3d5deadd6dfdb98d` (or exercise one of the enumerated unmigrated write paths, e.g. `FetchSourceBranch`/`UserCommitFiles`/`RebaseToRef`).
2. Concurrently issue two ordinary write RPCs (e.g. two concurrent `UserCommitFiles`/`RebaseToRef` calls, or a `FetchSourceBranch` racing a normal `git push`) that touch overlapping refs in the same repository on different Gitaly nodes.
3. Because neither transaction records `PREPARING_PHASE`, `lockRepoForTransaction` returns `nil` on the `PREPARED_PHASE`/`COMMITTED_PHASE` votes for both without ever acquiring `repoWriteLockMgr.Lock`, so both transactions proceed to lock on-disk refs on their respective primary nodes in potentially inconsistent order — reproducing the deadlock scenario described in `doc/serialized_writes.md`'s "Why Serialize Writes" section, i.e., the exact bug the feature was built to eliminate, silently reactivated by the untrusted/fallback interpretation of `phase`.

### Citations

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

**File:** proto/transaction.proto (L46-77)
```text
message VoteTransactionRequest {
  // Phase ...
  enum Phase {
    // UNKNOWN_PHASE is the unknown voting phase. This value has been the
    // default because phases have been introduced. Eventually, using this
    // phase will become unsupported.
    UNKNOWN_PHASE = 0; // protolint:disable:this ENUM_FIELD_NAMES_PREFIX ENUM_FIELD_NAMES_ZERO_VALUE_END_WITH
    // PREPARED_PHASE is the prepratory phase. The data that is about to change
    // is locked for concurrent modification, but changes have not yet been
    // written to disk.
    PREPARED_PHASE = 1; // protolint:disable:this ENUM_FIELD_NAMES_PREFIX
    // COMMITTED_PHASE is the committing phase. Data has been committed to disk
    // and will be visible in all subsequent requests.
    COMMITTED_PHASE  = 2; // protolint:disable:this ENUM_FIELD_NAMES_PREFIX
    // SYNCHRONIZED_PHASE is the synchronizing phase. This is used to synchronize nodes with each other on a
    // specific event.
    SYNCHRONIZED_PHASE = 3;  // protolint:disable:this ENUM_FIELD_NAMES_PREFIX
    // PREPARING_PHASE is the phase before PREPARED_PHASE. The data is not yet locked.
    PREPARING_PHASE = 4; // protolint:disable:this ENUM_FIELD_NAMES_PREFIX
  };

  // repository ...
  Repository repository = 1[(target_repository)=true];
  // transaction_id is the ID of the transaction we're processing.
  uint64 transaction_id = 2;
  // node is the name of the Gitaly node that's voting on a transaction.
  string node = 3;
  // reference_updates_hash is the SHA1 of the references that are to be updated.
  bytes reference_updates_hash = 4;
  // phase is the voting phase.
  Phase phase = 5;
}
```
