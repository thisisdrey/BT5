This confirms the analog. The documented behavior in `doc/transactions.md` explicitly states: "The assignment handling is fairly crude at the moment. The assignments are created before the repository is even created, and assignments are not deleted even if the repository is deleted." This matches the report's bug class exactly — a persisted state (partition assignment, analogous to `rootHash`) that once written via `setPartitionID` can never be corrected or removed through any RPC, and a mismatch permanently blocks further operations via `ErrRepositoriesAreInDifferentPartitions`.

### Title
Stale/incorrect partition assignments in `partitionAssignmentTable` are permanent and can permanently brick repository creation at a relative path - (File: internal/gitaly/storage/storagemgr/partition_assigner.go)

### Summary
Gitaly assigns every repository relative path to a storage partition on first access via `partitionAssigner.getPartitionID()`, persisting the mapping with `partitionAssignmentTable.setPartitionID()` [1](#0-0) . This assignment is written once and, per the project's own documentation, is never updated or deleted — even when the repository at that relative path is removed [2](#0-1) . Once a relative path's partition assignment disagrees with the partition required by a subsequent operation (e.g. connecting it to an object pool it wasn't originally hinted with), `getPartitionID()` unconditionally returns `ErrRepositoriesAreInDifferentPartitions` with no code path to reassign or clear the stale record [3](#0-2) .

### Finding Description
`partitionAssigner.getPartitionIDRecursive()` assigns a relative path to a partition exactly once, the first time it is referenced by any RPC, including repository-creation RPCs where the target does not yet exist on disk [4](#0-3) . If the relative path is later reused — for example, after `RemoveRepository` deletes the repository but leaves the `partitionAssignmentTable` entry intact (there is no `DeletePartitionAssignment`/similar cleanup call in the codebase) — a subsequent `CreateRepository`/`CreateFork`/`CreateObjectPool` at the same relative path will resolve to the assignment created for the *previous* incarnation of the repository, not the one appropriate for the new hint (`partitionWithRelativePath`) supplied by the caller [5](#0-4) .

Because `getPartitionID` fails with `ErrRepositoriesAreInDifferentPartitions` whenever the resolved partition doesn't match the hint's partition, any RPC that needs to relate the target repository to another repository in the request (an object-pool "alternate" hint) will permanently fail as long as the stale assignment exists, since nothing in the request-handling path can rewrite or delete a `partitionAssignmentTable` entry [6](#0-5) . This is architecturally identical to the reported Keystore.sol issue: an update function commits state that, if wrong, can never be corrected by any subsequent "update" call, permanently bricking the entity keyed by that identifier (relative path) with no built-in recovery RPC.

### Impact Explanation
This is reachable purely through ordinary Gitaly RPC usage — `RemoveRepository` followed by `CreateRepository`/`CreateFork`/`CreateObjectPool` using the same relative path (a "crafted RPC field", per an ordinary caller who controls `RelativePath`) — without any administrator privilege. The result is a permanent denial of service for that relative path: it can never again be successfully connected to (or disconnected from) an object pool through transactional RPCs, since the assignment mismatch check unconditionally errors out. Recovering requires out-of-band operator intervention directly against the embedded key-value store (no RPC exists to fix it), matching the "account becomes bricked, no recovery mechanism" bug class from the report.

### Likelihood Explanation
Likelihood depends on how often a relative path is reused with a different pooling relationship after a repository was deleted. In pure Gitaly (RPC-driven, not going through Rails) this is trivially reproducible by any client that can call `RemoveRepository`/`CreateFork`. Whether GitLab Rails reuses hashed-storage relative paths after project deletion in practice is a separate question, but Gitaly itself imposes no restriction and the maintainers' own documentation flags the assignment table as "crude" and acknowledges assignments are never cleaned up.

### Recommendation
Add a recovery/cleanup mechanism, e.g. delete or update the `partitionAssignmentTable` entry when a repository is removed (in `repoutil.Remove`/`RemoveRepository`), and/or expose an operator RPC to reassign or clear a partition assignment for a given relative path, mirroring the "accept data loss"/administrative recovery mechanisms that already exist for Praefect's generation tracking.

### Proof of Concept
1. Call `CreateRepository` for relative path `R` without any alternate hint → assigned to partition `P1` (`partitionAssignmentTable` entry `R -> P1`).
2. Call `RemoveRepository` for `R`. The repository directory is removed, but `partitionAssignmentTable` entry `R -> P1` is left in place (per `doc/transactions.md` lines 69-70 and absence of any deletion call in the codebase).
3. Call `CreateFork` (or `CreateObjectPool`) targeting relative path `R` again, this time supplying an origin/pool repository `O` that is (or gets) assigned to a different partition `P2`.
4. `getPartitionID(ctx, R, O, true)` resolves `R`'s existing stale assignment (`P1`) via `getPartitionIDRecursive`, compares it against the hint's partition `P2`, finds a mismatch, and returns `ErrRepositoriesAreInDifferentPartitions` [7](#0-6) .
5. Every future attempt to create/fork/pool a repository at relative path `R` with that pooling relationship fails identically forever, since nothing clears the stale assignment.

### Citations

**File:** internal/gitaly/storage/storagemgr/partition_assigner.go (L34-49)
```go
	initialPartitionID = 2
)

// relativePathNotFoundError is raised when attempting to assign a relative path that does not exist into
// a partition.
type relativePathNotFoundError string

func (err relativePathNotFoundError) Error() string {
	return fmt.Sprintf("relative path not found: %q", string(err))
}

// partitionAssignmentTable records which partitions repositories are assigned into.
type partitionAssignmentTable struct{ db keyvalue.Store }

func newPartitionAssignmentTable(db keyvalue.Store) *partitionAssignmentTable {
	return &partitionAssignmentTable{db: db}
```

**File:** internal/gitaly/storage/storagemgr/partition_assigner.go (L80-91)
```go
func (pt *partitionAssignmentTable) setPartitionID(relativePath string, id storage.PartitionID) error {
	wb := pt.db.NewWriteBatch()
	if err := wb.Set(pt.key(relativePath), id.MarshalBinary()); err != nil {
		return fmt.Errorf("set: %w", err)
	}

	if err := wb.Flush(); err != nil {
		return fmt.Errorf("flush: %w", err)
	}

	return nil
}
```

**File:** internal/gitaly/storage/storagemgr/partition_assigner.go (L151-177)
```go
// getPartitionID returns the partition ID of the repository. If the repository wasn't yet assigned into
// a partition, it will be assigned into one and the assignment stored. Further accesses return the stored
// partition ID. Repositories without an alternate go into their own partitions. Repositories with an alternate
// are assigned into the same partition as the alternate repository. The alternate is assigned into a partition
// if it hasn't yet been. The method is safe to call concurrently.
func (pa *partitionAssigner) getPartitionID(ctx context.Context, relativePath, partitionWithRelativePath string, isRepositoryCreation bool) (storage.PartitionID, error) {
	var partitionHint storage.PartitionID
	if partitionWithRelativePath != "" {
		var err error
		// See if the target repository itself is already in a partition. If so, we should assign the other repository
		// in the same partition if it is not yet partitioned.
		if partitionHint, err = pa.partitionAssignmentTable.getPartitionID(relativePath); err != nil {
			if !errors.Is(err, storage.ErrPartitionAssignmentNotFound) {
				return 0, fmt.Errorf("get possible partition id: %w", err)
			}

			// There was no assignment.
			partitionHint = 0
		}

		// Get or assign the alternate into a partition. If the target repository was already assigned into a partition,
		// assign the alternate in the same partition. The hinted repository should always exist already as it is an object pool, or
		// the origin repo of a fork.
		if partitionHint, err = pa.getPartitionIDRecursive(ctx, partitionWithRelativePath, false, partitionHint, false); err != nil {
			return 0, fmt.Errorf("get additional relative path's partition ID: %w", err)
		}
	}
```

**File:** internal/gitaly/storage/storagemgr/partition_assigner.go (L179-190)
```go
	// Get the repository's partition, or assign if it yet wasn't assigned, assign it with the alternate.
	ptnID, err := pa.getPartitionIDRecursive(ctx, relativePath, false, partitionHint, isRepositoryCreation)
	if err != nil {
		return 0, fmt.Errorf("get partition ID: %w", err)
	}

	if partitionHint != 0 && ptnID != partitionHint {
		return 0, ErrRepositoriesAreInDifferentPartitions
	}

	return ptnID, nil
}
```

**File:** internal/gitaly/storage/storagemgr/partition_assigner.go (L266-284)
```go
		// With the repository under lock, verify it is a Git directory before we assign it into a partition.
		// It's okay if the repository doesn't yet exist as this transaction may be about to create it.
		if err := storage.ValidateGitDirectory(filepath.Join(pa.storagePath, relativePath)); err != nil {
			if errors.Is(err, fs.ErrNotExist) {
				if !isRepositoryCreation {
					return 0, relativePathNotFoundError(relativePath)
				}

				// Repository creations are allowed to target non-existing repositories. They create the partition
				// where the repository is to be created.
			} else {
				return 0, fmt.Errorf("validate git directory: %w", err)
			}
		}

		ptnID, err = pa.assignPartitionID(ctx, relativePath, recursiveCall, partitionHint)
		if err != nil {
			return 0, fmt.Errorf("assign partition ID: %w", err)
		}
```

**File:** doc/transactions.md (L69-70)
```markdown
Partitioning is handled in the `StorageManager` component. This component handles routing transactions to the correct partitions, and starting and stopping partitions as needed. The partition assignments are retrieved or created at
[this location in the codebase](https://gitlab.com/gitlab-org/gitaly/-/blob/7c0f925b3df33c77de8c124b5f89447a13da3059/internal/gitaly/storage/storagemgr/partition_manager.go#L331) when a transaction is began against a repository for the first time. The assignment handling is fairly crude at the moment. The assignments are created before the repository is even created, and assignments are not deleted even if the repository is deleted.
```
