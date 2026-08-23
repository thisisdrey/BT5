### Title
Unprivileged pusher can indefinitely block repository deletion / migration via optimistic-concurrency conflict griefing - (File: internal/gitaly/storage/storagemgr/partition/transaction_manager_repo_test.go, internal/gitaly/storage/storagemgr/partition/conflict/manager.go)

### Summary
The external report describes a mutable, attacker-influenceable precondition (`LOCKER.balanceOf(address(this)) == 0`) that an unprivileged actor can perpetually violate with a trivial write ("dust" lock), permanently blocking a privileged withdrawal/migration action that requires that exact-zero state. In Gitaly's WAL transaction manager, repository deletion (and other exclusive repository-wide operations) is committed only if no conflicting write happened concurrently to any file/directory under the repository; a concurrent write to any file in the repository causes the deletion transaction to abort with a conflict error rather than being queued/retried by the manager itself.

### Finding Description
`RemoveRepository`/`repoutil.Remove` (via `TransactionManager`) deletes a repository by recording a `RemoveDirectoryEntry`/`RepositoryDeletion` WAL operation for every path in the repository [1](#0-0) [2](#0-1) . Conflict detection is optimistic: the `conflict.Manager` only checks whether the *repository itself* was concurrently deleted at a higher LSN, and the file-system history layer (`fshistory`) separately requires that every directory being removed is still empty and that every file being removed was not concurrently written between the transaction's read LSN and its commit [3](#0-2) .

Test cases in `transaction_manager_repo_test.go` confirm that if any ordinary write (e.g. a reference update by a normal user with push access) lands in the repository between the deletion transaction's snapshot read and its commit, the deletion fails outright with `fshistory.DirectoryNotEmptyError` or `fshistory.NewReadWriteConflictError`, and the repository state is left unchanged — the deletion is not retried automatically [4](#0-3) [5](#0-4) . Because an ordinary user with push access to the repository can issue lightweight reference updates at will (analogous to the "dust" `AuraLocker.lock` call in the report), they can race any admin/maintenance transaction that targets the whole repository (deletion, or other repository-wide exclusive operations such as pack-refs housekeeping, which is shown to conflict the same way with a concurrent deletion/write in `transaction_manager_housekeeping_test.go`) [6](#0-5) .

### Impact Explanation
An attacker with ordinary push permissions to a repository can perpetually abort administrative repository-deletion (or other repository-wide exclusive transactions) attempts by continuously submitting trivial reference updates timed to land within another transaction's read/commit window. Since the WAL transaction manager fails the operation outright on conflict instead of transparently retrying, a sufficiently persistent unprivileged actor can deny an administrator's ability to complete `RemoveRepository` or similar operations, mirroring the report's "no strategy migration is possible" outcome — the higher-privileged action never completes as long as the griefing continues.

### Likelihood Explanation
Likelihood is limited by the fact that: (1) the caller (e.g. `praefect remove-repository`, Rails, or an operator) will typically retry a failed RPC, and (2) winning the race window requires precise timing against another transaction's read/commit cycle, which shrinks as repository activity decreases. However, an attacker who controls automated push tooling can retry indefinitely and cheaply (a single ref update per attempt), making sustained denial-of-service against repository deletion/maintenance plausible for as long as the attacker retains write access, which is a realistic assumption for typical Gitaly deployments (any project maintainer/CI credential).

### Recommendation
- Consider having the transaction manager internally retry conflicting repository-deletion (and other repository-wide/exclusive) transactions a bounded number of times, or serialize such exclusive operations against a per-repository write lock instead of relying purely on optimistic conflict detection, so that a stream of unprivileged writes cannot indefinitely veto a privileged whole-repository operation.
- Alternatively, provide an explicit "quiesce" mode for `RemoveRepository` that temporarily rejects new writes to the target repository once deletion has been requested, closing the window during which griefing writes can land.
- Add metrics/alerting on repeated deletion conflict aborts for the same relative path to detect this griefing pattern operationally.

### Proof of Concept
1. Two clients hold write access to the same repository: an operator (or automation) issuing `RemoveRepository`, and an attacker with ordinary push access.
2. Operator begins a transaction to delete the repository (`Begin{TransactionID: N, RelativePaths: [repo]}`).
3. Before the deletion transaction commits, the attacker submits a trivial reference update (e.g., `refs/heads/branch`) that commits first, exactly as reproduced by the test scenario "deletion fails with concurrent write introducing files in repository" [5](#0-4) .
4. The deletion transaction then fails to commit with `fshistory.DirectoryNotEmptyError` on `refs/heads`, and the repository remains fully intact.
5. The attacker repeats step 3 on every subsequent `RemoveRepository` retry, indefinitely denying repository removal as long as they retain push access.

### Citations

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager_housekeeping.go (L788-807)
```go
		if isDir {
			// If this is a directory, we need to ensure it is actually empty before removing
			// it. Check if we find any directory entries we haven't yet deleted.
			entries, err := os.ReadDir(mgr.getAbsolutePath(relativePath))
			if err != nil {
				return fmt.Errorf("read dir: %w", err)
			}

			for _, entry := range entries {
				if _, ok := deletedPaths[filepath.Join(relativePath, entry.Name())]; ok {
					// This path was already deleted. Don't consider it to exist.
					continue
				}

				// This directory was not empty because someone concurrently wrote
				// a reference into it. Keep it in place.
				directoriesToKeep[relativePath] = struct{}{}
				return nil
			}
		}
```

**File:** internal/gitaly/storage/storagemgr/partition/conflict/manager.go (L70-99)
```go
// Prepare prepares the transaction for a commit. It checks the transaction for conflicts introduced
// by other concurrent transactions Once the transaction is prepared, is guarantee to commit successfully.
func (mgr *Manager) Prepare(ctx context.Context, tx *Transaction) (*PreparedTransaction, error) {
	defer trace.StartRegion(ctx, "Prepare").End()

	// Conflict check this transaction.
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

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager_repo_test.go (L963-1008)
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
```

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager_repo_test.go (L1009-1044)
```go
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
