### Title
Attacker-triggered repository creation on a chosen path can pre-assign a stray partition, permanently blocking future fork/object-pool creation on that path - ([File: internal/gitaly/storage/storagemgr/partition_assigner.go])

### Summary
Gitaly assigns a repository's relative path to a storage partition the first time it is touched, even before the repository itself exists, so that repository-creating RPCs can begin a transaction. This assignment write is not atomic with the actual repository creation. Anyone able to invoke a repository-creating RPC against a chosen `relativePath` (e.g. `CreateRepository`, `CreateFork`, `CreateRepositoryFromURL`) can cause that relative path to be permanently and irrevocably assigned to a partition, and if a subsequently attempted, legitimate operation (e.g. a fork or an object-pool link) requires that same relative path to be co-located with a different partition (its origin/pool), the operation is permanently rejected with `ErrRepositoriesAreInDifferentPartitions`. This mirrors the referenced dTRINITY bug class: a cheap, unprivileged action that permanently pollutes shared accounting/assignment state and blocks all future legitimate use of that state.

### Finding Description
`beginTransactionForRepository` explicitly special-cases repository-creating RPCs to allow assigning a partition for a relative path that doesn't yet correspond to an actual repository: [1](#0-0) 

The code comment acknowledges the root cause directly: the partition assignment is created before the repository, the two are not atomic, and a failed creation can leave a stale assignment behind (tracked as gitlab-org/gitaly#5957), with no cleanup mechanism implemented.

The assignment itself is written straight to the embedded key-value store as soon as the relative path is first seen, independent of whether the eventual repository creation succeeds: [2](#0-1) 

Crucially, when a caller later needs to co-locate two relative paths in the same partition (e.g. a fork with its origin repository, or a repository with its object pool), the assigner looks up any pre-existing assignment for the target relative path and compares it against the partition hint derived from the other, already-partitioned repository: [3](#0-2) 

If the relative path was previously (and permanently) assigned to a different partition than the hint, `getPartitionID` unconditionally returns `ErrRepositoriesAreInDifferentPartitions` — every time, for as long as the stray assignment exists, which per the design is forever, since assignments are never deleted even when a repository is deleted: [4](#0-3) 

Because `CreateFork` and `CreateObjectPool` deliberately place a not-yet-existing fork/pool member into the same partition as its origin/pool via this exact mechanism (documented behavior), any attacker who can drive a repository-creating RPC against a specific target relative path before the legitimate fork/pool operation runs — and then let/force that creation to fail (invalid seed data, bad remote URL, killed connection, malformed snapshot, etc.) — leaves that relative path permanently assigned to an unrelated partition. All subsequent legitimate attempts to create the "real" fork/pool member at that path will hit the partition mismatch and fail forever, with no self-healing path in the code.

### Impact Explanation
This is a persistent, unprivileged denial-of-service against a specific repository path: once the relative path is poisoned with a wrong partition assignment, it can never again be validly created as a fork/object-pool member co-located with its intended origin, since the assignment table is authoritative and never garbage-collected. This is directly analogous to the dTRINITY report's "attacker breaks the entire contract by front-running with 1 wei": a tiny, cheap, unprivileged action (triggering and then failing a repository-creation RPC) permanently corrupts shared bookkeeping state (the partition assignment table) that a subsequent, legitimate operation's correctness check depends on, causing that check to fail unconditionally from then on.

### Likelihood Explanation
Repository-creating RPCs (`CreateRepository`, `CreateRepositoryFromURL`, `CreateRepositoryFromSnapshot`, `CreateRepositoryFromBundle`, `CreateFork`) are reachable through ordinary GitLab actions (import, fork, mirror) where the caller can choose or predict the destination relative path (e.g. by controlling the project/namespace slug used to derive it), and causing such an RPC to fail partway through (bad URL, malformed archive, connection drop) is trivial and requires no special privilege. The stale-assignment condition itself is explicitly called out as a known, unresolved issue in the code (gitlab-org/gitaly#5957), increasing confidence that the underlying non-atomicity is real and currently unmitigated.

### Recommendation
Make partition assignment atomic with (or reversible on failure of) repository creation: either defer persisting the assignment until the repository creation transaction commits, or record failed/aborted creations and roll back (delete) the stray partition assignment for that relative path so it can be re-assigned correctly on a later, successful attempt. At minimum, provide an idempotent repair/cleanup path (e.g. in housekeeping or a `praefect`/`gitaly` maintenance subcommand) that detects and clears partition assignments for relative paths that were never backed by an actual repository.

### Proof of Concept
1. As an ordinary user, trigger a repository-creating RPC (e.g. `CreateRepositoryFromURL`) targeting relative path `P`, using a URL/config guaranteed to fail after the transaction/partition assignment has begun (e.g. an unreachable remote or malformed snapshot archive), per `beginTransactionForRepository`'s special-casing of `isRepositoryCreation` RPCs (`internal/gitaly/storage/storagemgr/middleware.go:363-371`). This causes `getPartitionIDRecursive` to assign `P` a fresh partition ID and persist it via `partitionAssignmentTable.setPartitionID` (`internal/gitaly/storage/storagemgr/partition_assigner.go:234-284`, `:80-91`) even though the actual repository is never created.
2. Later, a legitimate operation (e.g. `CreateFork` with source repository `S` already assigned to partition `X`) attempts to create the real repository at path `P`, expecting it to be assigned into `S`'s partition `X` via the partition hint mechanism (`internal/gitaly/storage/storagemgr/partition_assigner.go:156-190`).
3. Because `P` is already permanently assigned to the attacker-induced partition from step 1, `getPartitionID` returns `ErrRepositoriesAreInDifferentPartitions` at line 185-187, and the fork/pool-linking operation fails — and will keep failing indefinitely since nothing ever clears the stale assignment.

### Citations

**File:** internal/gitaly/storage/storagemgr/middleware.go (L363-371)
```go
	// Begin fails when attempting to access a repository that doesn't exist and doesn't have a partition
	// assignment yet. Repository creating RPCs are an exception and are allowed to create the partition
	// assignment so the transaction can begin, and the repository can be created. The partition assignments
	// are created before the repository is created and are thus not atomic. Failed creations may leave stale
	// partition assignments in the key-value store. We'll later make the repository and partition assignment
	// creations atomic.
	//
	// See issue: https://gitlab.com/gitlab-org/gitaly/-/issues/5957
	_, isRepositoryCreation := repositoryCreatingRPCs[methodInfo.FullMethodName()]
```

**File:** internal/gitaly/storage/storagemgr/partition_assigner.go (L156-190)
```go
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

**File:** internal/gitaly/storage/storagemgr/partition_assigner.go (L234-284)
```go
func (pa *partitionAssigner) getPartitionIDRecursive(ctx context.Context, relativePath string, recursiveCall bool, partitionHint storage.PartitionID, isRepositoryCreation bool) (storage.PartitionID, error) {
	// Check first whether the repository is already assigned into a partition. If so, just return the assignment.
	ptnID, err := pa.partitionAssignmentTable.getPartitionID(relativePath)
	if err != nil {
		if !errors.Is(err, storage.ErrPartitionAssignmentNotFound) {
			return 0, fmt.Errorf("get partition: %w", err)
		}

		// Repository wasn't yet assigned into a partition. This is the slow path. Requests attempting
		// to get or assign a partition ID concurrently are serialized.

		releaseLock, err := pa.acquireRepositoryLock(ctx, relativePath)
		if err != nil {
			return 0, fmt.Errorf("acquire repository lock: %w", err)
		}

		defer releaseLock()

		// With the repository locked, check first whether someone else assigned it into a partition
		// while we weren't holding the lock between the first failed attempt getting the assignment
		// and locking the repository.
		ptnID, err = pa.partitionAssignmentTable.getPartitionID(relativePath)
		if !errors.Is(err, storage.ErrPartitionAssignmentNotFound) {
			if err != nil {
				return 0, fmt.Errorf("recheck partition: %w", err)
			}

			// Some other goroutine assigned a partition between the failed attempt and locking the
			// repository.
			return ptnID, nil
		}

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
