### Title
Cross-Repository Object Access via Unauthenticated `gitaly-partitioning-hint` gRPC Metadata - ([File: internal/gitaly/storage/context.go])

### Summary
The Nextcloud report describes a copy operation whose path resolution logic conflates a shared file's virtual location with another user's entire underlying storage tree, exposing all of that user's data. The closest reachable analog in Gitaly is the WAL-partitioning "hint" mechanism: an ordinary caller of a mutator RPC can set the `gitaly-partitioning-hint` gRPC metadata key to an arbitrary relative path, and Gitaly's transaction middleware will use it, without any ownership or relationship check, to force the caller's target repository into the *same storage partition and transaction snapshot* as the attacker-chosen "hint" repository.

### Finding Description
`ExtractPartitioningHint`/`ContextWithPartitioningHint` read and write a plain, unauthenticated gRPC metadata key `gitaly-partitioning-hint`: [1](#0-0) 

`beginTransactionForRepository` (invoked for every mutator/accessor RPC via the storage manager middleware) reads this hint straight from incoming metadata and uses it, **for any target repository**, as the "alternate" relative path to co-partition with — with no check that the target repository is actually a fork or pool member of that hint, and no check that the caller is even authorized to know that hint path exists: [2](#0-1) 

This alternate relative path is then passed straight into `StorageManager.Begin`, which calls `partitionAssigner.getPartitionID` with the hint as `partitionWithRelativePath`, and — crucially — includes **both** the target and the hinted relative path in the transaction's `RelativePaths`, meaning the resulting snapshot is built to contain both repositories: [3](#0-2) 

`getPartitionID` will assign the hinted repository's existing partition ID to the (usually newly-created) target repository if the target has no partition yet, joining them permanently in the same partition: [4](#0-3) 

This mechanism exists intentionally so that legitimate flows (e.g. `CreateFork`, or Praefect relaying a `relative-path-bin`/hint header after resolving replica paths) place forks and their object pool in the same partition, as documented: [5](#0-4) 

However, nothing in `ExtractPartitioningHint` distinguishes a hint that was legitimately derived by Gitaly/Praefect internal logic (e.g. from a validated `CreateForkRequest.SourceRepository`) from one supplied directly by the calling client as raw gRPC metadata — it is just an incoming metadata lookup with no cryptographic or provenance binding. Any client able to issue a mutator RPC that creates or first-partitions a repository can set this header to reference an arbitrary victim relative path in the same storage, causing the victim repository to be pulled into the attacker's transaction snapshot (analogous to the Nextcloud copy operation pulling in the entire other user's file tree instead of only the shared file).

### Impact Explanation
If exploited, an attacker-controlled repository is silently co-located in the same WAL partition/snapshot as a victim repository chosen entirely by the attacker via metadata, without any relationship (alternate/pool link) actually being established through the audited `objectpool`/`Link` code path (`internal/git/objectpool/link.go`), which normally enforces the alternates-file linking. Once co-partitioned, the two repositories' snapshots and internal representations share the write-ahead log/partition storage described in `doc/transactions.md`, giving a path to cross-repository data exposure and to abuse the `LinkRepositoryToObjectPool` flow (which only requires the two repos be in the same partition, per `internal/gitaly/storage/storagemgr/middleware_ext_test.go` `TestMiddleware_partitioning_hint`) to gain read access to the victim's objects via alternates. This matches the required "cross-repository object access" / "object-pool and alternates isolation" bug class.

### Likelihood Explanation
Requires an attacker who can issue authenticated mutator RPCs against Gitaly directly (i.e., has valid Gitaly gRPC access, as any ordinary GitLab user's push/fork/import requests ultimately do), and needs to know or guess the victim's relative path (these are often predictable `@hashed/xx/yy/hash.git` paths derived from project IDs, which are frequently enumerable). In a Praefect-fronted deployment, Praefect itself sets this hint from validated request fields for `CreateFork`/`ReplicateRepository`, reducing exposure; but at the Gitaly layer itself the header is not validated as coming only from Praefect, and any direct/other caller can supply it, since `beginTransactionForRepository` treats any non-empty incoming metadata value as authoritative.

### Recommendation
Do not trust a bare, attacker-suppliable metadata key for partition assignment. Bind the partitioning hint to already-authorized/derived repository fields (e.g., only accept it when it is programmatically set by Gitaly's own internal code from a validated additional-repository field, and reject/ignore externally supplied `gitaly-partitioning-hint` metadata at the server boundary, similar to how quarantine directories are only trusted when running in scope of a transaction). At minimum, verify that the hinted relative path is actually related to the target repository (e.g. is its declared alternate, or is the `SourceRepository` of the exact same `CreateForkRequest`) before using it to co-partition, rather than accepting any string from incoming metadata.

### Proof of Concept
1. As an authenticated Gitaly client, discover (or brute-force) the relative path of a victim repository, e.g. `@hashed/aa/bb/victim.git`.
2. Send a mutator RPC that creates a new repository (e.g. `CreateRepository`) for an attacker-owned relative path `@hashed/cc/dd/attacker.git`, attaching gRPC metadata:
   `gitaly-partitioning-hint: @hashed/aa/bb/victim.git`
3. Per `beginTransactionForRepository` (internal/gitaly/storage/storagemgr/middleware.go:311-318), Gitaly treats the victim path as the "alternate" to co-partition with, and `StorageManager.Begin` (internal/gitaly/storage/storagemgr/partition_manager.go:380-390) includes both relative paths in the transaction's snapshot.
4. If the victim repository had no assigned partition yet, or attacker can force ordering, `getPartitionID` (internal/gitaly/storage/storagemgr/partition_assigner.go:151-190) assigns the attacker's new repository into the victim's partition.
5. Follow up with `LinkRepositoryToObjectPool` between attacker's repo and the victim's repo (now valid because they share a partition, as confirmed by `TestMiddleware_partitioning_hint` in internal/gitaly/storage/storagemgr/middleware_ext_test.go:21-153), gaining alternates-based read access to the victim's objects.

*Note: I could not fully verify from the indexed code whether an additional server-side interceptor (outside what I retrieved) strips or validates the `gitaly-partitioning-hint` header specifically for non-Praefect/non-internal callers, nor could I confirm the exact authorization boundary of which callers can reach Gitaly directly with arbitrary metadata versus only through Praefect. Confirming this would require a live/dynamic test or reviewing additional interceptor configuration not present in the indexed snippets.*

### Citations

**File:** internal/gitaly/storage/context.go (L80-111)
```go
const keyPartitioningHint = "gitaly-partitioning-hint"

// ContextWithPartitioningHint stores the relativePath as a partitioning hint into the incoming
// gRPC metadata in the context.
func ContextWithPartitioningHint(ctx context.Context, relativePath string) context.Context {
	md, ok := metadata.FromIncomingContext(ctx)
	if !ok {
		md = metadata.New(nil)
	} else {
		md = md.Copy()
	}
	md.Set(keyPartitioningHint, relativePath)

	return metadata.NewIncomingContext(ctx, md)
}

// ExtractPartitioningHint extracts the partitioning hint from the incoming gRPC
// metadata in the context. Empty string is returned if no partitioning hint was provided.
// An error is returned if the metadata in the context contained multiple partitioning hints.
func ExtractPartitioningHint(ctx context.Context) (string, error) {
	relativePaths := metadata.ValueFromIncomingContext(ctx, keyPartitioningHint)
	if len(relativePaths) > 1 {
		return "", errors.New("multiple partitioning hints")
	}

	if len(relativePaths) == 0 {
		// No partitioning hint was set.
		return "", nil
	}

	return relativePaths[0], nil
}
```

**File:** internal/gitaly/storage/storagemgr/middleware.go (L307-330)
```go
	var (
		alternateStorageName  string
		alternateRelativePath string
	)
	if hint, err := storage.ExtractPartitioningHint(ctx); err != nil {
		return transactionalizedRequest{}, fmt.Errorf("extract partitioning hint: %w", err)
	} else if hint != "" {
		// In some cases a repository needs to be partitioned with a repository that isn't set as an additional
		// repository in the request. If so, a partitioning hint is sent through the gRPC metadata to provide
		// the relative path of the repository the target repository should be partitioned with.
		alternateStorageName = targetRepo.GetStorageName()
		alternateRelativePath = hint
	} else if req, ok := req.(*gitalypb.CreateForkRequest); ok {
		// We use the source repository of a CreateForkRequest implicitly as a partitioning hint as we know the source
		// repository and the fork must be placed in the same partition in order to join them to the same pool. Source
		// repository is not marked as an additional repository so it doesn't get rewritten by Praefect. The original
		// form is needed in the handler as Gitaly fetches the source repository through Praefect's API which needs
		// the original repository to route the request correctly.
		//
		// The implicit hinting here avoids having to add hints at every callsite. We only do this if no explicit
		// partitioning hint was provided as Praefect provides an explicit hint with CreateForkRequest.
		alternateStorageName = req.GetSourceRepository().GetStorageName()
		alternateRelativePath = req.GetSourceRepository().GetRelativePath()
	}
```

**File:** internal/gitaly/storage/storagemgr/partition_manager.go (L341-396)
```go
// Begin gets the Partition for the specified repository and starts a transaction. If a
// Partition is not already running, a new one is created and used. The partition tracks
// the number of pending transactions and this counter gets incremented when Begin is invoked.
func (sm *StorageManager) Begin(ctx context.Context, opts storage.TransactionOptions) (_ storage.Transaction, returnedErr error) {
	if opts.RelativePath == "" {
		return nil, fmt.Errorf("target relative path unset")
	}

	relativePath, err := storage.ValidateRelativePath(sm.path, opts.RelativePath)
	if err != nil {
		return nil, structerr.NewInvalidArgument("validate relative path: %w", err)
	}

	partitionID, err := sm.partitionAssigner.getPartitionID(ctx, relativePath, opts.AlternateRelativePath, opts.AllowPartitionAssignmentWithoutRepository)
	if err != nil {
		if errors.Is(err, badger.ErrDBClosed) {
			// The database is closed when PartitionManager is closing. Return a more
			// descriptive error of what happened.
			return nil, ErrPartitionManagerClosed
		}

		return nil, fmt.Errorf("get partition: %w", err)
	}

	ctx = storage.ContextWithPartitioningHint(ctx, relativePath)

	ptn, err := sm.startPartition(ctx, partitionID)
	if err != nil {
		return nil, err
	}

	defer func() {
		if returnedErr != nil {
			// Close the partition handle on error as the caller wouldn't do so anymore by
			// committing/rollbacking the transaction.
			ptn.Close()
		}
	}()

	relativePaths := []string{relativePath}
	if opts.AlternateRelativePath != "" {
		relativePaths = append(relativePaths, opts.AlternateRelativePath)
	}

	transaction, err := ptn.Begin(ctx, storage.BeginOptions{
		Write:                            !opts.ReadOnly,
		RelativePaths:                    relativePaths,
		ForceExclusiveSnapshot:           opts.ForceExclusiveSnapshot,
		SkipPreventingReftableCompaction: opts.SkipPreventingReftableCompaction,
	})
	if err != nil {
		return nil, err
	}

	return newFinalizableTransaction(transaction, ptn.Close), nil
}
```

**File:** internal/gitaly/storage/storagemgr/partition_assigner.go (L151-190)
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

**File:** doc/transactions.md (L56-70)
```markdown
Gitaly automatically assigns repositories to partitions when they are first accessed:

- Object pools and all repositories connected to the object pool are placed in the same partition. Repositories that are about to be connected to an object pool, such as newly created forks, are also placed in the same partition with the object pool they are about to be connected.
  - Assigning pools and their connected repositories into the same partition ensures transactions can guarantee consistency between them. If pools were in different partitions, transaction ordering could cause issues, for example updating a reference in a fork before the objects are written into the pool.
- Repositories that are not connected (nor about to be connected) to an object pool are placed in their own partitions.

Whether repository is, or is about to be connected to an object pool, is determined from multiple sources:

- If it has an `info/alternates` file, the repository is currently connected to a pool.
- If it is an RPC that contains an additional repository in the request, the repository is about to be connected.
  - For example, `CreateFork` contains the origin repository as an additional repository. We place the fork in the same partition as an objet pool might be created, and the fork and the origin connected to it.
  - `CreateObjectPool` will similarly place the object pool into the same partition as the source repository since it will generally be connected to it.

Partitioning is handled in the `StorageManager` component. This component handles routing transactions to the correct partitions, and starting and stopping partitions as needed. The partition assignments are retrieved or created at
[this location in the codebase](https://gitlab.com/gitlab-org/gitaly/-/blob/7c0f925b3df33c77de8c124b5f89447a13da3059/internal/gitaly/storage/storagemgr/partition_manager.go#L331) when a transaction is began against a repository for the first time. The assignment handling is fairly crude at the moment. The assignments are created before the repository is even created, and assignments are not deleted even if the repository is deleted.
```
