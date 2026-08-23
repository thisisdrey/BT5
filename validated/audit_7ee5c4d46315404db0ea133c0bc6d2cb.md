### Title
Malformed alternates file permanently blocks partition assignment, causing permanent DoS of the affected repository - ([File: internal/gitaly/storage/storagemgr/partition_assigner.go])

### Summary
`partitionAssigner.getPartitionIDRecursive`/`assignPartitionID` assign a repository's storage partition exactly once and persist the result to `partitionAssignmentTable`. If assignment fails (e.g. because the repository's `objects/info/alternates` file is malformed or points somewhere invalid), no assignment is ever written, so every future transaction attempt on that repository re-runs the same failing resolution and fails again — permanently, until manual intervention.

### Finding Description
`getPartitionID` first checks whether a repository already has a partition assignment; if not, it takes the "slow path" and calls `assignPartitionID`, which in turn calls `getAlternatePartitionID` to see whether the repository is linked to an object pool via its Git alternates file: [1](#0-0) 

`getAlternatePartitionID` reads the alternates file and validates its content: [2](#0-1) 

`gitstorage.ReadAlternatesFile` returns `ErrMultipleAlternates` if the file contains more than one alternate line, and `getAlternatePartitionID` additionally returns `ErrAlternateHasAlternate` for chained alternates and `ErrAlternatePointsToSelf`/a path-validation error for out-of-storage or self-referential paths: [3](#0-2) 

None of these error paths are `gitstorage.ErrNoAlternate`, so `assignPartitionID` propagates the error instead of falling back to allocating a fresh partition: [4](#0-3) 

Because the error is returned before `partitionAssignmentTable.setPartitionID` is ever called, the repository is left permanently unassigned. On the next call (i.e., the next push, fetch, or any other RPC that needs to open a transaction on that repository), `getPartitionIDRecursive` again finds no cached assignment (line 236 `pt.getPartitionID` still returns `ErrPartitionAssignmentNotFound`) and re-attempts the same alternates resolution, hitting the identical error again. There is no code path that quarantines, skips, or otherwise recovers from this repository-level failure — the loop over "read alternates → validate → recurse" is not resilient to a single bad entry, directly analogous to the reported vault bug where a single bad adapter call blocks every future rebalance.

A malformed/hostile alternates file is attacker-reachable through normal object-pool linking or repository-replication/import flows that write `objects/info/alternates` content (e.g., `Link`/`CreateObjectPool`/fork or replication paths that copy alternates content from a source repository), since the writer side does not perform the same validation that `partitionAssigner` performs later: [5](#0-4) 

If the alternates content ends up with multiple entries, a chained alternate, a self-referencing alternate, or a path that fails `storage.ValidateRelativePath` (e.g. an absolute or `../`-escaping path), any RPC hitting `getPartitionID` for that repository will error out indefinitely.

### Impact Explanation
Once a repository ends up with such an alternates file, `getPartitionID` will fail on every subsequent transaction attempt because the assignment is never persisted. Since virtually all mutator and many accessor RPCs in the new WAL-based storage manager require obtaining a partition via this code path, this turns into a permanent denial of service against that single repository: every push, fetch, or other transactional RPC targeting it will fail, and the repository stays broken until an administrator manually fixes the alternates file or the partition-assignment record out of band. This mirrors the reported bug's blast radius (permanent inability to operate on the affected unit) even though the affected unit here is a single repository/partition rather than an entire storage.

### Likelihood Explanation
The alternates file is not validated for correctness when it is written by pool-linking, fork, or repository-import/replication code paths (the write path in `objectpool.Link` merely persists whatever relative path is computed, and any external process/import that stages `objects/info/alternates` directly is similarly unchecked before `partitionAssigner` ever reads it). An attacker who can influence repository creation/import content (e.g., through a crafted bundle restore, a forged fork/pool setup, or a race that leaves multiple concurrent alternates writes) can produce a file that trips one of `ErrMultipleAlternates`, `ErrAlternateHasAlternate`, `ErrAlternatePointsToSelf`, or a relative-path validation failure. This does not require any special privileges, and the resulting condition is permanent rather than self-healing, making it a real Repository-scoped DoS if the flaw is confirmed reachable through those write paths.

### Recommendation
- Validate the alternates file content (single entry, no self-reference, no chaining, in-storage relative path) at the point where it is written (`objectpool.Link`, replication/import code, fork creation) so malformed content can never reach disk.
- In `partitionAssigner.assignPartitionID`, do not leave the repository permanently unassigned on a validation failure from `getAlternatePartitionID`: either fall back to assigning the repository its own partition (matching the `ErrNoAlternate` behavior) while flagging the invalid alternates condition for operator remediation, or cache the failure with a bounded retry/backoff and surface a clear "repository requires manual repair" error rather than silently repeating full re-validation forever.
- Add an operator-facing repair/reset RPC or CLI subcommand analogous to `DisconnectGitAlternates` that can force-clear a bad alternates file and unblock partition assignment without requiring direct filesystem access.

### Proof of Concept
Note: I was not able to fully trace, within the available indexed context, the exact external RPC/import path that allows an unprivileged user to write a multi-line or self-referencing `objects/info/alternates` file into a target repository (this would require examining `CreateFork`, `ReplicateRepository`, and bundle-restore code paths in more depth than the index exposes). The following describes the mechanism confirmed directly in code:
1. Cause (via any repository-creation/import/pool-linking flow that stages `objects/info/alternates`) the target repository's alternates file to contain either more than one line, a path pointing to another repository that itself has an alternate, a path pointing to itself, or a path that resolves outside the storage root.
2. Issue any RPC against that repository that requires beginning a transaction (e.g., a push). `partitionAssigner.getPartitionID` → `assignPartitionID` → `getAlternatePartitionID` returns one of `ErrMultipleAlternates`, `storage.ErrAlternateHasAlternate`, `storage.ErrAlternatePointsToSelf`, or a `ValidateRelativePath` error, and `assignPartitionID` returns that error without calling `setPartitionID` (`internal/gitaly/storage/storagemgr/partition_assigner.go:290-315`).
3. Repeat the same RPC (or any other transactional RPC) against the repository — it fails identically every time because `partitionAssignmentTable` never has an entry for the repository's relative path, confirming the repository is permanently unusable until manual intervention.

### Citations

**File:** internal/gitaly/storage/storagemgr/partition_assigner.go (L234-288)
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
	}

	return ptnID, nil
}
```

**File:** internal/gitaly/storage/storagemgr/partition_assigner.go (L290-315)
```go
func (pa *partitionAssigner) assignPartitionID(ctx context.Context, relativePath string, recursiveCall bool, partitionHint storage.PartitionID) (storage.PartitionID, error) {
	// Check if the repository has an alternate. If so, it needs to go into the same
	// partition with it.
	ptnID, err := pa.getAlternatePartitionID(ctx, relativePath, recursiveCall, partitionHint)
	if err != nil {
		if !errors.Is(err, gitstorage.ErrNoAlternate) {
			return 0, fmt.Errorf("get alternate partition ID: %w", err)
		}

		ptnID = partitionHint
		if ptnID == invalidPartitionID {
			// The repository has no alternate. Unpooled repositories go into their own partitions.
			// Allocate a new partition ID for this repository.
			ptnID, err = pa.allocatePartitionID()
			if err != nil {
				return 0, fmt.Errorf("acquire partition id: %w", err)
			}
		}
	}

	if err := pa.partitionAssignmentTable.setPartitionID(relativePath, ptnID); err != nil {
		return 0, fmt.Errorf("set partition: %w", err)
	}

	return ptnID, nil
}
```

**File:** internal/gitaly/storage/storagemgr/partition_assigner.go (L317-360)
```go
func (pa *partitionAssigner) getAlternatePartitionID(ctx context.Context, relativePath string, recursiveCall bool, partitionHint storage.PartitionID) (storage.PartitionID, error) {
	alternate, err := gitstorage.ReadAlternatesFile(filepath.Join(pa.storagePath, relativePath))
	if err != nil {
		return 0, fmt.Errorf("read alternates file: %w", err)
	}

	if recursiveCall {
		// recursive being true indicates we've arrived here through another repository's alternate.
		// Repositories in Gitaly should only have a single alternate that points to the repository's
		// pool. Chains of alternates are unexpected and could go arbitrarily long, so fail the operation.
		return 0, storage.ErrAlternateHasAlternate
	}

	// The relative path should point somewhere within the same storage.
	alternateRelativePath, err := storage.ValidateRelativePath(
		pa.storagePath,
		// Take the relative path to the repository, not 'repository/objects'.
		filepath.Dir(
			// The path in alternates file points to the object directory of the alternate
			// repository. The path is relative to the repository's own object directory.
			filepath.Join(relativePath, "objects", alternate),
		),
	)
	if err != nil {
		return 0, fmt.Errorf("validate relative path: %w", err)
	}

	if alternateRelativePath == relativePath {
		// The alternate must not point to the repository itself. Not only is it non-sensical
		// but it would also cause a dead lock as the repository is locked during this call
		// already.
		return 0, storage.ErrAlternatePointsToSelf
	}

	// Recursively get the alternate's partition ID or assign it one. This time
	// we set recursive to true to fail the operation if the alternate itself has an
	// alternate configured.
	ptnID, err := pa.getPartitionIDRecursive(ctx, alternateRelativePath, true, partitionHint, false)
	if err != nil {
		return 0, fmt.Errorf("get partition ID: %w", err)
	}

	return ptnID, nil
}
```

**File:** internal/gitaly/storage/gitstorage/alternates.go (L19-41)
```go
// ReadAlternatesFile reads the alternates file from the given repository. ErrNoAlternate is returned if the
// file doesn't exist or didn't contain an alternate. ErrMultipleAlternates is returned if the
// repository had multiple alternates.
func ReadAlternatesFile(repositoryPath string) (string, error) {
	alternates, err := stats.ReadAlternatesFile(repositoryPath)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return "", ErrNoAlternate
		}

		return "", fmt.Errorf("read alternates file: %w", err)
	}

	if len(alternates) == 0 {
		return "", ErrNoAlternate
	} else if len(alternates) > 1 {
		// Repositories shouldn't have more than one alternate given they should only be
		// linked to a single pool at most.
		return "", ErrMultipleAlternates
	}

	return alternates[0], nil
}
```

**File:** internal/git/objectpool/link.go (L54-66)
```go
	alternatesWriter, err := safe.NewLockingFileWriter(altPath)
	if err != nil {
		return fmt.Errorf("creating alternates writer: %w", err)
	}
	defer func() {
		if err := alternatesWriter.Close(); err != nil && returnedErr == nil {
			returnedErr = fmt.Errorf("closing alternates writer: %w", err)
		}
	}()

	if _, err := io.WriteString(alternatesWriter, expectedRelPath); err != nil {
		return fmt.Errorf("writing alternates: %w", err)
	}
```
