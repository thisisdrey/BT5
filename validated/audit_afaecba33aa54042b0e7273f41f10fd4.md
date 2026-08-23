### Title
Unbounded, permanent partition-assignment records let an ordinary user OOM/disk-exhaust a Gitaly node via repeated repository-creating RPCs - ([File: internal/gitaly/storage/storagemgr/partition_assigner.go])

### Summary
Every RPC that can create a repository (`CreateRepository`, `CreateFork`, `CreateRepositoryFromURL`, `CreateRepositoryFromBundle`, `CreateRepositoryFromSnapshot`, `ReplicateRepository`, `CreateObjectPool`) causes Gitaly to assign the target relative path to a partition *before* the repository is even created on disk, and this assignment is never removed even if repository creation fails or the repository is later deleted. There is no limit on how many distinct relative paths (i.e., distinct partition-assignment entries) a single caller can register. This is the same bug class as the libp2p-rendezvous report: a per-caller-controlled, uniquely-keyed resource (`namespace` there, `relativePath` here) that is validated only for content/format, never for count, and that persists indefinitely without any cleanup or throttling hook.

### Finding Description
`partitionAssigner.getPartitionID` (and its recursive helper) is invoked from the transaction middleware (`internal/gitaly/storage/storagemgr/middleware.go`) whenever a transactional RPC targets a repository. For the RPCs listed in `repositoryCreatingRPCs` [1](#0-0) , `isRepositoryCreation` is set to `true`, which skips the on-disk `ValidateGitDirectory` check and lets the assigner mint a brand-new `PartitionID` and persist a `(relativePath -> partitionID)` mapping in the embedded key-value store even though the repository does not yet exist: [2](#0-1) 

The mapping is written unconditionally via `partitionAssignmentTable.setPartitionID`, keyed only by the caller-supplied `relativePath` string, with a monotonically incrementing `PartitionID` sequence (`badger.Sequence`) — there is no cap on the number of relative paths that may be assigned, and no per-user/per-request accounting: [3](#0-2) [4](#0-3) 

The project's own documentation confirms the lack of cleanup: "The assignment handling is fairly crude at the moment. The assignments are created before the repository is even created, and assignments are not deleted even if the repository is deleted." [5](#0-4) 

This mirrors the rendezvous bug precisely: (1) each unique key (`namespace` / `relativePath`) gets its own persistent entry, (2) entries are only validated for content, not count, (3) nothing reclaims the entry once the "registration" (fork/import attempt) is abandoned or the created repository is deleted, and (4) the `DefaultMaxInactivePartitions` LRU only bounds the number of *active in-memory partition runtime objects* [6](#0-5)  — it does not bound the number of *partition-assignment records* in the KV store, nor the `idSequence` badger counter, nor eventually the number of on-disk WAL/partition directories that get created under `+gitaly/partitions/...` once creation actually proceeds.

### Impact Explanation
Any ordinary GitLab user capable of triggering repository creation, fork, or import operations (which map to the RPCs in `repositoryCreatingRPCs`) can repeatedly submit requests with unique target relative paths. Each request permanently grows the partition-assignment key space in the storage's embedded KV database and consumes a partition ID from the shared sequence, regardless of whether the underlying repository creation ultimately succeeds, fails, or is later deleted. Sustained abuse leads to unbounded growth of the KV store and, once actual repository/partition directories are created, unbounded on-disk partition/WAL directory growth — a resource-exhaustion (disk/memory) Denial of Service against the Gitaly node, consistent with CWE-770 (Allocation of Resources Without Limits or Throttling), the same weakness class as the reported libp2p-rendezvous issue.

### Likelihood Explanation
Likelihood is high for any deployment that exposes fork/import/repository-creation actions to regular (non-privileged) users, since no special permissions beyond normal project-creation/fork/import rights are required, and the vulnerable code path is unconditionally executed by the transaction middleware for every request to the affected RPCs. No authentication bypass or privilege escalation is needed — only the ability to invoke ordinary "create repository"/"fork"/"import" actions repeatedly with distinct target paths.

### Recommendation
Introduce a per-caller/per-storage bound on the number of partition assignments (or a rate limit on repository-creating RPCs) analogous to the fix proposed for the rendezvous server: track and cap the number of distinct relative-path assignments a caller can create within a time window, reclaim/garbage-collect partition-assignment entries whose backing repository no longer exists (mirroring how deleted repositories should also release their assignment), and consider bounding the lifetime of "pending" assignments created for not-yet-existing repositories so that abandoned creation attempts do not permanently consume KV store space and partition ID sequence numbers.

### Proof of Concept
Conceptual PoC (not executed against a live instance): repeatedly call `CreateRepository` (or `CreateFork`/`CreateRepositoryFromBundle`/etc.) with a unique `relative_path` on each call, then optionally call `RemoveRepository` immediately afterward. Each iteration causes `partitionAssigner.getPartitionID` to run the `isRepositoryCreation` branch and permanently write a new `(relativePath, partitionID)` entry via `setPartitionID` [3](#0-2) , which is never removed by `RemoveRepository`. Looping this a large number of times (analogous to the "namespace" loop in the rendezvous report) grows the KV store and partition ID sequence without bound and without any server-side pushback, since no limiter guards this specific code path (unlike the RPC/repo concurrency limiters documented in `doc/backpressure.md`, which only throttle in-flight RPCs, not the number of distinct partition assignments created).

### Citations

**File:** internal/gitaly/storage/storagemgr/middleware.go (L63-72)
```go
// repositoryCreatingRPCs are all of the RPCs that may create a repository.
var repositoryCreatingRPCs = map[string]struct{}{
	gitalypb.ObjectPoolService_CreateObjectPool_FullMethodName:             {},
	gitalypb.RepositoryService_CreateFork_FullMethodName:                   {},
	gitalypb.RepositoryService_CreateRepository_FullMethodName:             {},
	gitalypb.RepositoryService_CreateRepositoryFromURL_FullMethodName:      {},
	gitalypb.RepositoryService_CreateRepositoryFromBundle_FullMethodName:   {},
	gitalypb.RepositoryService_CreateRepositoryFromSnapshot_FullMethodName: {},
	gitalypb.RepositoryService_ReplicateRepository_FullMethodName:          {},
}
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

**File:** doc/transactions.md (L69-70)
```markdown
Partitioning is handled in the `StorageManager` component. This component handles routing transactions to the correct partitions, and starting and stopping partitions as needed. The partition assignments are retrieved or created at
[this location in the codebase](https://gitlab.com/gitlab-org/gitaly/-/blob/7c0f925b3df33c77de8c124b5f89447a13da3059/internal/gitaly/storage/storagemgr/partition_manager.go#L331) when a transaction is began against a repository for the first time. The assignment handling is fairly crude at the moment. The assignments are created before the repository is even created, and assignments are not deleted even if the repository is deleted.
```

**File:** internal/gitaly/config/config.go (L53-54)
```go
	// DefaultMaxInactivePartitions is the default number of inactive partitions to keep on standby.
	DefaultMaxInactivePartitions = uint(100)
```
