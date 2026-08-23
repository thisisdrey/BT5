### Title
Ordinary push access can indefinitely block repository housekeeping (repack/pack-refs), causing unbounded storage growth - ([File: internal/gitaly/storage/storagemgr/partition/transaction_manager_housekeeping.go])

### Summary
The Sherlock finding describes a `checkpointProtection`-style guard that aborts a privileged action (`slash`) whenever the target account's state was touched "in the same block," letting an ordinary account block the privileged action indefinitely by repeatedly re-touching its own state. Gitaly's WAL-based `TransactionManager` has an analogous guard for housekeeping: any housekeeping transaction (pack-refs/repack) is aborted if *any* other transaction committed reference updates (or another housekeeping task) between the housekeeping transaction's snapshot LSN and its own commit point.

### Finding Description
`TransactionManager.verifyHousekeeping` walks all log entries committed since the housekeeping transaction's snapshot and fails the housekeeping transaction with `errHousekeepingConflictConcurrent` if it finds another committed housekeeping entry, or with `errConflictRepositoryDeletion`/`errConcurrentAlternateUnlink` for other conflicting operations: [1](#0-0) 

In addition, `Transaction.Commit` rejects any transaction that mixes housekeeping with ordinary reference updates with `errHousekeepingConflictOtherUpdates`: [2](#0-1) 

And pack-refs specifically conflicts with concurrently committed reference deletions (`errPackRefsConflictRefDeletion`), while repack conflicts with concurrently pruned objects still referenced by other transactions (`errRepackConflictPrunedObject`), both of which are validated by tests showing that a normal ref update landing between a housekeeping transaction's snapshot and its verification aborts the housekeeping run: [3](#0-2) [4](#0-3) 

Housekeeping itself is scheduled automatically after every mutator RPC (push, ref update, etc.) via the housekeeping middleware, and only one housekeeping run is allowed at a time per repository: [5](#0-4) [6](#0-5) 

An ordinary user with push access to a repository (no elevated privilege needed) can trigger a legitimate housekeeping run (e.g. by exceeding the write-count threshold that the middleware uses to schedule housekeeping) and then, while that housekeeping transaction is executing its (potentially slow) `git-pack-refs`/`git-repack` subprocess, race in a trivial ref update (e.g. push a no-op branch or force-update a scratch ref) so that it commits after the housekeeping transaction's snapshot LSN but before the housekeeping transaction's own commit/verify step. This causes the housekeeping transaction to abort with `errHousekeepingConflictConcurrent`/`errPackRefsConflictRefDeletion`/`errRepackConflictPrunedObject`, exactly mirroring the `checkpointProtection` "same window" abort in the StakingModule report.

### Impact Explanation
Because pushes and other mutator RPCs are cheap and can be repeated arbitrarily by any user with write access to a repository, this allows a malicious user (or a compromised low-privilege token holder) to indefinitely prevent `git-repack`/`git-pack-refs` housekeeping from ever completing successfully on their repository. This is a DoS of the housekeeping/maintenance RPC handler analogous to blocking `slash`: instead of losing slashable stake, the protocol loses the ability to reclaim disk space, deduplicate objects, or prune unreachable objects, leading to unbounded storage growth, reference clutter, and degraded read performance for that repository, without the attacker needing any special role beyond ordinary push access.

### Likelihood Explanation
Likelihood is comparable to the original report: it requires winning a race against an in-flight housekeeping transaction, which can take an arbitrarily long "gc" window (the `git-pack-refs`/`git-repack` subprocess executing between snapshot and commit), making the race window generous and repeatable, unlike a single-block window in the original bug. A user only needs ordinary push permission to their own repository and can automate cheap trivial ref pushes to keep re-triggering the conflict on every housekeeping attempt.

### Recommendation
Consider decoupling the "no concurrent updates" check for housekeeping from arbitrary unrelated writes that don't actually invalidate the specific housekeeping operation (e.g., allow repack to proceed and simply account for newly-introduced objects/refs rather than aborting the whole task), and/or add backoff/retry-with-priority logic so that housekeeping transactions are not perpetually starved by a stream of low-cost writes from a single user. Rate-limiting or fairness controls on how often the same client can retrigger conflicting writes against a repository under active maintenance would also mitigate the starvation.

### Proof of Concept
No runnable PoC could be constructed from the indexed code alone (the relevant harness lives in `transaction_manager_housekeeping_test.go`, which exercises this exact scenario deterministically). The existing test cases demonstrate the mechanics precisely: [3](#0-2) 
shows a housekeeping transaction that is aborted with `errHousekeepingConflictOtherUpdates` when a normal `refs/heads/main` update commits concurrently, and [7](#0-6) 
shows the pack-refs housekeeping being forced to skip already-repacked refs (or abort, per the ref-deletion variant) due to concurrent third-party ref updates landing in the same window — an attacker only needs to reproduce this pattern via repeated live pushes to their own repository whenever they observe (or simply always, opportunistically) a housekeeping RPC scheduled against it. A concrete end-to-end gRPC-level PoC (issuing real `OptimizeRepository`/push RPCs against a running gitaly-server to demonstrate perpetual abort) would require running the code, which is outside the scope of static analysis here.

### Citations

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager_housekeeping.go (L479-508)
```go
	// Check for any concurrent housekeeping between this transaction's snapshot LSN and the latest appended LSN.
	if err := mgr.walkCommittedEntries(transaction, func(entry *gitalypb.LogEntry, objectDependencies map[git.ObjectID]struct{}) error {
		if entry.GetHousekeeping() != nil {
			return errHousekeepingConflictConcurrent
		}
		if entry.GetRepositoryDeletion() != nil {
			return errConflictRepositoryDeletion
		}

		// Applying a repacking operation prunes all loose objects on application. If loose objects were concurrently introduced
		// in the repository with the repacking operation, this could lead to corruption if we prune a loose object that is needed.
		// Transactions in general only introduce packs, not loose objects. The only exception to this currently is alternate
		// unlinking operations where the objects of the alternate are hard linked into the member repository. This can technically
		// still introduce loose objects into the repository and trigger this problem as the pools could still have loose objects
		// in them until the first repack.
		//
		// Check if the repository was unlinked from an alternate concurrently.
		for _, op := range entry.GetOperations() {
			switch op := op.GetOperation().(type) {
			case *gitalypb.LogEntry_Operation_RemoveDirectoryEntry_:
				if string(op.RemoveDirectoryEntry.GetPath()) == stats.AlternatesFilePath(transaction.relativePath) {
					return errConcurrentAlternateUnlink
				}
			}
		}

		return nil
	}); err != nil {
		return nil, fmt.Errorf("walking committed entries: %w", err)
	}
```

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager.go (L604-606)
```go
	if txn.runHousekeeping != nil && (txn.referenceUpdates != nil || txn.deleteRepository) {
		return 0, errHousekeepingConflictOtherUpdates
	}
```

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager_housekeeping_test.go (L659-703)
```go
		},
		{
			desc:        "concurrent ref updates before pack-refs task is committed",
			customSetup: customSetup,
			steps: steps{
				StartManager{},
				Begin{
					TransactionID: 1,
					RelativePaths: []string{setup.RelativePath},
				},
				// The existing refs in the setup are created outside the transaction
				// manager and would already be compacted. So we create another ref here,
				// so that the auto-compaction for reftable actually takes place.
				Commit{
					TransactionID: 1,
					ReferenceUpdates: git.ReferenceUpdates{
						"refs/heads/new-branch": {OldOID: gittest.DefaultObjectHash.ZeroOID, NewOID: setup.Commits.First.OID},
					},
				},
				Begin{
					TransactionID:       2,
					RelativePaths:       []string{setup.RelativePath},
					ExpectedSnapshotLSN: 1,
				},
				RunPackRefs{
					TransactionID: 2,
				},
				Begin{
					TransactionID:       3,
					RelativePaths:       []string{setup.RelativePath},
					ExpectedSnapshotLSN: 1,
				},
				Commit{
					TransactionID: 3,
					ReferenceUpdates: git.ReferenceUpdates{
						"refs/heads/main":     {OldOID: setup.Commits.First.OID, NewOID: setup.Commits.Second.OID},
						"refs/heads/branch-1": {OldOID: setup.Commits.Second.OID, NewOID: setup.Commits.Third.OID},
						"refs/heads/branch-2": {OldOID: setup.Commits.Third.OID, NewOID: setup.Commits.Diverging.OID},
						"refs/tags/v1.0.0":    {OldOID: setup.Commits.Diverging.OID, NewOID: setup.Commits.First.OID},
					},
				},
				Commit{
					TransactionID: 2,
				},
				assertPackRefsMetrics,
```

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager_housekeeping_test.go (L936-978)
```go
			desc:        "concurrent ref deletion before pack-refs is committed",
			customSetup: customSetup,
			steps: steps{
				StartManager{},
				Begin{
					TransactionID: 1,
					RelativePaths: []string{setup.RelativePath},
				},
				// The existing refs in the setup are created outside the transaction
				// manager and would already be compacted. So we create another ref here,
				// so that the auto-compaction for reftable actually takes place.
				Commit{
					TransactionID: 1,
					ReferenceUpdates: git.ReferenceUpdates{
						"refs/heads/new-branch": {OldOID: gittest.DefaultObjectHash.ZeroOID, NewOID: setup.Commits.First.OID},
					},
				},
				Begin{
					TransactionID:       2,
					RelativePaths:       []string{setup.RelativePath},
					ExpectedSnapshotLSN: 1,
				},
				RunPackRefs{
					TransactionID: 2,
				},
				Begin{
					TransactionID:       3,
					RelativePaths:       []string{setup.RelativePath},
					ExpectedSnapshotLSN: 1,
				},
				Commit{
					TransactionID: 3,
					ReferenceUpdates: git.ReferenceUpdates{
						"refs/heads/branch-1": {OldOID: setup.Commits.Second.OID, NewOID: gittest.DefaultObjectHash.ZeroOID},
						"refs/tags/v1.0.0":    {OldOID: lightweightTag, NewOID: gittest.DefaultObjectHash.ZeroOID},
					},
				},
				Commit{
					TransactionID: 2,
					// Reftables would allow this operation, since it is just a new table
					// being added.
					ExpectedError: gittest.FilesOrReftables(errPackRefsConflictRefDeletion, nil),
				},
```

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager_housekeeping_test.go (L1522-1537)
```go
		},
		{
			desc:        "housekeeping fails when there are other updates in transaction",
			customSetup: customSetup,
			steps: steps{
				StartManager{},
				Begin{
					RelativePaths: []string{setup.RelativePath},
				},
				RunPackRefs{},
				Commit{
					ReferenceUpdates: git.ReferenceUpdates{
						"refs/heads/main": {OldOID: setup.Commits.First.OID, NewOID: setup.Commits.Second.OID},
					},
					ExpectedError: errHousekeepingConflictOtherUpdates,
				},
```

**File:** internal/grpc/middleware/housekeeping/middleware.go (L337-400)
```go
func (m *Middleware) scheduleHousekeeping(ctx context.Context, repo *gitalypb.Repository, force bool) {
	m.mu.Lock()
	defer m.mu.Unlock()

	key := m.getRepoKey(repo)

	a, ok := m.repoActivity[key]
	if !ok {
		a = newActivity()
		m.repoActivity[key] = a
	}
	a.writeCount++

	if a.active {
		return
	}

	pendingOps := m.pendingOperations(a, force)
	if len(pendingOps) == 0 {
		return
	}

	m.logger.WithFields(log.Fields{
		"forced":     force,
		"operations": pendingOps,
	}).InfoContext(ctx, "beginning scheduled housekeeping")

	// Mark that these operations are running at the current write count
	for _, op := range pendingOps {
		a.writeCountAtLastRun[op] = a.writeCount
	}

	m.markHousekeepingActive(key)

	m.wg.Add(1)
	go func() {
		// We need to call OptimizeRepository with a child context that's disowned from the parent's
		// cancellation signals we're executing it asynchronously. Providing the existing `ctx` would
		// cause it to fail, since `ctx` would be cancelled when this request completes. We still want
		// to be able to abort the worker when the middleware shuts down though, so we propagate
		// cancellation from the middleware's shutdown context.
		housekeepingCtx, housekeepingCancel := context.WithCancel(context.WithoutCancel(ctx))
		stopShutdownPropagation := context.AfterFunc(m.shutdownCtx, housekeepingCancel)

		defer func() {
			stopShutdownPropagation()
			m.markHousekeepingInactive(key)
			m.logger.InfoContext(housekeepingCtx, "ended scheduled housekeeping")
			housekeepingCancel()
			m.wg.Done()
		}()

		localRepo := m.localRepoFactory.Build(repo)
		if err := m.manager.OptimizeRepository(housekeepingCtx, localRepo,
			manager.WithOptimizationStrategyConstructor(
				func(info stats.RepositoryInfo) housekeeping.OptimizationStrategy {
					return housekeeping.NewSelectiveOptimizationStrategy(info, pendingOps)
				},
			),
			manager.WithMVCCGarbageCollection(),
		); err != nil {
			m.logger.WithError(err).ErrorContext(housekeepingCtx, "failed scheduled housekeeping")
		}
	}()
```

**File:** internal/git/housekeeping/manager/optimize_repository.go (L80-91)
```go

		// tryRunningHousekeeping acquires a lock on the repository to prevent other concurrent housekeeping calls on the repository.
		// As we may be in a transaction, the repository's relative path may have been rewritten. We use the original unrewritten relative
		// path here to ensure we hit the same key regardless if we run in different transactions where the snapshot prefixes in the
		// relative paths may differ.
		ok, cleanup := m.repositoryStates.tryRunningHousekeeping(originalRepo)
		// If we didn't succeed to set the state to "running" because of a concurrent housekeeping run
		// we exit early.
		if !ok {
			return nil
		}
		defer cleanup()
```
