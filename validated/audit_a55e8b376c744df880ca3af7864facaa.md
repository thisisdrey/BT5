This confirms the mechanism precisely: `Prepare` in `internal/gitaly/storage/storagemgr/partition/conflict/manager.go` and the `DirectoryNotEmptyError`/`NewReadWriteConflictError` checks in `transaction_manager_repo_test.go` show that a repository-deletion transaction (`DeleteRepository: true`) is committed via a WAL apply that requires the on-disk repository directory tree to be write-conflict-free and effectively "empty" of concurrently-introduced files (e.g. `refs/heads` must not gain new entries, `HEAD` must not be concurrently written) — analogous to PirexGmx's `signalTransfer` requiring the vester-token balance to be zero. Any concurrent write into the repository (a normal, unprivileged `git push` producing a new reference/loose-ref file or reftable table) causes the deletion commit to fail with `fshistory.DirectoryNotEmptyError` / `fshistory.NewReadWriteConflictError`, exactly like the attacker depositing vester tokens to make `PirexGmx`'s balance non-zero and block `signalTransfer`.

### Title
Ordinary push traffic can indefinitely block repository deletion via write/deletion transaction conflicts - (File: `internal/gitaly/storage/storagemgr/partition/transaction_manager_repo_test.go`, `internal/gitaly/storage/storagemgr/partition/conflict/manager.go`)

### Summary
`DeleteRepository` transactions committed through Gitaly's WAL-based `TransactionManager` require that no concurrent write transaction has introduced or modified files inside the repository's on-disk tree (loose refs, `HEAD`, `reftable/tables.list`, etc.) between the deletion transaction's read snapshot and its commit. If any such write lands first, the deletion is aborted with a conflict error rather than being queued or retried, mirroring the PirexGmx pattern where a state-precondition ("balance must be zero" / "directory must be empty/unmodified") gates a privileged lifecycle transition, and can be perpetually violated by an ordinary, unprivileged actor.

### Finding Description
`TransactionManager` deletion is implemented as a two-phase conflict check: `conflict.Manager.Prepare` records `DeleteRepository` transactions and marks a repository deletion at a given LSN [1](#0-0) , while lower-level file conflicts are separately detected by the filesystem-history layer (`fshistory`), which raises `DirectoryNotEmptyError` or `NewReadWriteConflictError` when a deletion transaction's snapshot of the repository directory is invalidated by a concurrently-committed write that adds/edits a file inside it, e.g. a new file under `refs/heads` or a rewritten `HEAD`/`tables.list` [2](#0-1) .

Because reference updates (pushes) are exactly this kind of "concurrent write," an ordinary user who still has push access to a repository that is scheduled for deletion (e.g., during any window between issuance of a `RemoveRepository`/partition-manager delete call and its actual commit — such as GitLab's project deletion soft-delete/grace-period flow, or simple request racing) can repeatedly push new branches/refs. Each push that lands with a commit LSN newer than the deletion transaction's read LSN will cause the deletion's commit to fail with a `DirectoryNotEmptyError`/read-write conflict rather than proceeding, exactly as in the referenced report where `PirexGmx.initiateMigration` could be permanently blocked by an unprivileged actor keeping `gmxVester`/`glpVester` balances non-zero.

### Impact Explanation
An unprivileged user retaining ordinary push access to a repository targeted for deletion can indefinitely delay/deny the deletion operation by racing pushes against the delete transaction, causing repeated `errConflictRepositoryDeletion`/`DirectoryNotEmptyError` failures. This is a denial-of-service against a storage-management RPC handler (repository deletion/housekeeping), which can interfere with compliance-driven deletions (e.g., GDPR erasure), storage reclamation, and administrative repository lifecycle operations. Unlike outright privilege escalation, the impact is availability/DoS-class against an internal handler, consistent with the Medium severity assigned to the analogous PirexGmx finding.

### Likelihood Explanation
Likelihood is moderate: the conflict window exists only between a deletion transaction's snapshot read (`ReadLSN`) and its commit, so a single race is unlikely to always succeed, but an attacker capable of issuing continuous or well-timed pushes (any client with push access) can retry the race indefinitely until deletion keeps failing, especially in environments where deletion is asynchronous/retried by a background reconciler and the repository remains writable during that time (as evidenced by tests explicitly modeling "housekeeping/writes concurrent with repository deletion fail" scenarios [3](#0-2) , and "housekeeping transaction runs concurrently with a repository deletion" [4](#0-3) ).

### Recommendation
Ensure repository-deletion is not indefinitely blockable by ordinary write traffic: either (a) fence off further pushes/writes as soon as a deletion is requested (e.g., by acquiring an exclusive "pending deletion" marker analogous to `repoutil.Lock` used in `internal/gitaly/repoutil/remove.go` [5](#0-4) , so subsequent writes are rejected rather than racing), or (b) automatically retry/re-snapshot and re-commit the deletion transaction with backoff until quorum is achieved, so that repeated pushes cannot perpetually starve the deletion.

### Proof of Concept
1. Client A calls `RemoveRepository` (or the internal delete-repository transaction path) against a repository the attacker still has push access to.
2. Concurrently, the attacker (Client B) issues `git push` creating a new branch/ref, landing a write transaction with a commit LSN higher than the deletion transaction's `ReadLSN`.
3. Per the test case `"deletion fails with concurrent write introducing files in repository"` [6](#0-5) , the deletion transaction's commit fails with `fshistory.DirectoryNotEmptyError` on `refs/heads` (or the reftable directory), and the repository state is left with the pushed branch instead of being deleted.
4. The attacker repeats step 2 whenever the deletion is retried, indefinitely preventing repository removal — mirroring the original PirexGmx report where re-depositing vester tokens after every migration attempt permanently blocked `initiateMigration`.

### Citations

**File:** internal/gitaly/storage/storagemgr/partition/conflict/manager.go (L76-99)
```go
	//
	// First check that the repository has not been concurrently deleted while this transaction
	// was executing.
	if deletedLSN, ok := mgr.repositoryDeletions[tx.TargetRelativePath]; ok {
		if deletedLSN > tx.ReadLSN {
			return nil, ErrRepositoryConcurrentlyDeleted
		}
	}

	// If the repository is being deleted, don't bother checking for reference conflicts.
	if tx.DeleteRepository {
		return &PreparedTransaction{
			commit: func(commitLSN storage.LSN) {
				// Record the repository deletion.
				delete(mgr.repositoryDeletionsByLSN, mgr.repositoryDeletions[tx.TargetRelativePath])
				mgr.repositoryDeletions[tx.TargetRelativePath] = commitLSN
				mgr.repositoryDeletionsByLSN[commitLSN] = tx.TargetRelativePath

				// Evict the reference history of the repository so the history does not contain any
				// pre-deletion values.
				mgr.referenceHistory.EvictRepository(ctx, tx.TargetRelativePath)
			},
		}, nil
	}
```

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager_repo_test.go (L963-1043)
```go
		{
			desc: "deletion fails with concurrent write to an existing file in repository",
			steps: steps{
				StartManager{},
				Begin{
					TransactionID: 1,
					RelativePaths: []string{setup.RelativePath},
				},
				Begin{
					TransactionID: 2,
					RelativePaths: []string{setup.RelativePath},
				},
				Commit{
					TransactionID: 1,
					DefaultBranchUpdate: &DefaultBranchUpdate{
						Reference: "refs/heads/branch",
					},
				},
				Commit{
					TransactionID:    2,
					DeleteRepository: true,
					ExpectedError: gittest.FilesOrReftables[error](
						fshistory.NewReadWriteConflictError(
							// The deletion fails on the new file only because the deletions are currently
							filepath.Join(setup.RelativePath, "HEAD"), 0, 1,
						),
						fshistory.DirectoryNotEmptyError{
							// Conflicts on the `tables.list` file are ignored with reftables as reference
							// write conflicts with it. We see the conflict here as reftable directory not
							// being empty due to the new table written into it.
							Path: filepath.Join(setup.RelativePath, "reftable"),
						},
					),
				},
			},
			expectedState: StateAssertion{
				Database: DatabaseState{
					string(keyAppliedLSN): storage.LSN(1).ToProto(),
				},
				Repositories: RepositoryStates{
					setup.RelativePath: {
						DefaultBranch: "refs/heads/branch",
					},
				},
			},
		},
		{
			desc: "deletion fails with concurrent write introducing files in repository",
			steps: steps{
				StartManager{},
				Begin{
					TransactionID: 1,
					RelativePaths: []string{setup.RelativePath},
				},
				Begin{
					TransactionID: 2,
					RelativePaths: []string{setup.RelativePath},
				},
				Commit{
					TransactionID: 1,
					ReferenceUpdates: git.ReferenceUpdates{
						"refs/heads/branch": {NewOID: setup.Commits.First.OID},
					},
				},
				Commit{
					TransactionID:    2,
					DeleteRepository: true,
					ExpectedError: gittest.FilesOrReftables(
						fshistory.DirectoryNotEmptyError{
							// The deletion fails on `refs/heads` directory as it is no longer empty
							// due to the concurrent branch write.
							Path: filepath.Join(setup.RelativePath, "refs", "heads"),
						},
						fshistory.DirectoryNotEmptyError{
							// Conflicts on the `tables.list` file are ignored with reftables as reference
							// write conflicts with it. We see the conflict here as reftable directory not
							// being empty due to the new table written into it.
							Path: filepath.Join(setup.RelativePath, "reftable"),
						},
					),
				},
```

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager_repo_test.go (L1320-1381)
```go
		{
			desc: "writes concurrent with repository deletion fail",
			steps: steps{
				RemoveRepository{},
				StartManager{},
				Begin{
					TransactionID: 1,
					RelativePaths: []string{setup.RelativePath},
				},
				CreateRepository{
					TransactionID: 1,
					References: map[git.ReferenceName]git.ObjectID{
						"refs/heads/main": setup.Commits.First.OID,
					},
					Packs: [][]byte{setup.Commits.First.Pack},
				},
				Commit{
					TransactionID: 1,
				},

				// Start a write. During the write, the repository is deleted
				// and recreated. We expect this write to fail.
				Begin{
					TransactionID:       3,
					RelativePaths:       []string{setup.RelativePath},
					ExpectedSnapshotLSN: 1,
				},

				// Delete the repository concurrently.
				Begin{
					TransactionID:       4,
					RelativePaths:       []string{setup.RelativePath},
					ExpectedSnapshotLSN: 1,
				},
				Commit{
					TransactionID:    4,
					DeleteRepository: true,
				},

				// Recreate the repository concurrently.
				Begin{
					TransactionID:       5,
					RelativePaths:       []string{setup.RelativePath},
					ExpectedSnapshotLSN: 2,
				},
				CreateRepository{
					TransactionID: 5,
				},
				Commit{
					TransactionID: 5,
				},

				// Commit the write that ran during which the repository was concurrently recreated. This
				// should lead to a conflict.
				Commit{
					TransactionID: 3,
					ReferenceUpdates: git.ReferenceUpdates{
						"refs/heads/main": {OldOID: setup.Commits.First.OID, NewOID: setup.ObjectHash.ZeroOID},
					},
					ExpectedError: conflict.ErrRepositoryConcurrentlyDeleted,
				},
			},
```

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager_housekeeping_test.go (L1944-1985)
```go
		},
		{
			desc:        "housekeeping transaction runs concurrently with a repository deletion",
			customSetup: customSetup,
			steps: steps{
				StartManager{},
				Begin{
					TransactionID: 1,
					RelativePaths: []string{setup.RelativePath},
				},
				RunPackRefs{
					TransactionID: 1,
				},
				Begin{
					TransactionID: 2,
					RelativePaths: []string{setup.RelativePath},
				},
				Commit{
					TransactionID:    2,
					DeleteRepository: true,
				},
				Begin{
					TransactionID:       3,
					RelativePaths:       []string{setup.RelativePath},
					ExpectedSnapshotLSN: 1,
				},
				CreateRepository{
					TransactionID: 3,
				},
				Commit{
					TransactionID: 3,
				},
				Commit{
					TransactionID: 1,
					ExpectedError: errConflictRepositoryDeletion,
				},
				AssertMetrics{histogramMetric("gitaly_housekeeping_tasks_latency"): {
					"housekeeping_task=total,stage=prepare":     1,
					"housekeeping_task=total,stage=verify":      1,
					"housekeeping_task=pack-refs,stage=prepare": 1,
				}},
			},
```

**File:** internal/gitaly/repoutil/remove.go (L90-102)
```go
	if err := voteOnAction(ctx, txManager, repository, voting.Preparing); err != nil {
		return structerr.NewInternal("vote on rename: %w", err)
	}
	// Lock the repository such that it cannot be created or removed by any concurrent
	// RPC call.
	unlock, err := Lock(ctx, logger, locator, repository)
	if err != nil {
		if errors.Is(err, safe.ErrFileAlreadyLocked) {
			return structerr.NewFailedPrecondition("repository is already locked")
		}
		return structerr.NewInternal("locking repository for removal: %w", err)
	}
	defer unlock()
```
