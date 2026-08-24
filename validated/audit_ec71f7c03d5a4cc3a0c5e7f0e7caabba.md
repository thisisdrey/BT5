### Title
Unauthenticated `gitaly-partitioning-hint` gRPC metadata allows cross-repository partition co-location - (File: internal/gitaly/storage/context.go, internal/gitaly/storage/storagemgr/middleware.go, internal/gitaly/storage/storagemgr/partition_assigner.go)

### Summary
Gitaly's transaction/partition manager lets a caller influence which storage partition a newly created repository is assigned to via a plain gRPC metadata field, `gitaly-partitioning-hint`. This hint is read directly from the incoming gRPC metadata without any check that the caller is authorized to reference the hinted repository, and without verifying that an actual on-disk relationship (an alternates file) exists between the two repositories. For a freshly-created repository this hint is accepted as-is and used to place the new repository into the same WAL partition as an arbitrary, unrelated (and possibly private) repository.

### Finding Description
The hint is stored/read as ordinary incoming metadata with no cryptographic binding or provenance check: [1](#0-0) 

`beginTransactionForRepository` uses this hint to compute `alternateRelativePath` for the transaction, choosing it over the implicit `CreateFork`/`ObjectPoolService` derivations only when no additional repository is present in the request: [2](#0-1) 

The resulting `AlternateRelativePath` is passed into `StorageManager.Begin`, which calls `partitionAssigner.getPartitionID` with the hinted relative path as `partitionWithRelativePath`: [3](#0-2) 

Inside `getPartitionID`/`assignPartitionID`, the hinted repository's partition is fetched (or assigned) first, and the *target* (new) repository is assigned a partition. Crucially, when the target repository is being created it does not yet have an `objects/info/alternates` file, so `getAlternatePartitionID` returns `gitstorage.ErrNoAlternate`, and the code falls back to using `partitionHint` (the value derived from the attacker-supplied metadata field) directly as the new repository's partition ID: [4](#0-3) [5](#0-4) 

There is no check anywhere in this call chain that the hinted relative path is actually the source of a `CreateFork`, an object pool the caller is entitled to link to, or otherwise related to the caller/target repository — the mechanism only distinguishes an *explicit* hint from an *implicit* `CreateForkRequest`-derived one, and only rejects the combination of an explicit hint with an "additional repository" field (`ErrPartitioningHintAndAdditionalRepoProvided`), not an unrelated/unauthorized hint value: [6](#0-5) 

Because `ContextWithPartitioningHint` merely calls `metadata.Set` on a normal incoming-context, this is functionally equivalent to any other gRPC request metadata field a client is capable of sending on a repository-creation mutator call reaching Gitaly's transaction middleware.

### Impact Explanation
Placing two logically unrelated repositories in the same storage partition breaks the isolation model that object pools/alternates isolation is designed to enforce (this is explicitly one of the flagged unprivileged-analog impact classes: object-pool/alternates isolation and cross-repository object access). A partition owns a shared WAL, shared per-partition KV namespace, and shared snapshot/staging lifecycle; forcing an attacker-controlled new repository into a victim's partition risks: WAL log-entry/transaction interference between unrelated repositories sharing the same partition handle, corrupting or blocking the victim repository's transaction processing (DoS of the RPC handler / partition), and creating an on-disk state inconsistency where the partition assignment table records a shared partition ID that the filesystem alternates chain does not actually support — a state the code elsewhere explicitly treats as invalid (`ErrAlternateHasAlternate`, `ErrAlternatePointsToSelf`) but does not defend against here because the fresh repository simply has no alternates file yet to contradict the hint.

### Likelihood Explanation
The primary code path (`CreateFork`) legitimately allows the hint mechanism to be exercised without an additional-repository field, meaning the general acceptance of unauthenticated hints is already a live, reachable feature for ordinary repository-creation RPCs, not merely an internal Praefect-only construct. An attacker who can invoke a repository-creation mutator RPC (`CreateRepository`, `CreateFork`, `CreateObjectPool`, etc.) directly against Gitaly and knows (or can guess) the relative path of a victim repository can attach the metadata key to co-opt that repository's partition. Exploitation requires no race/front-running window since the check is a straightforward metadata-driven assignment, making it more reliable than the referenced report's mempool front-running scenario.

### Recommendation
Require that the target repository's relationship to the hinted repository be independently verified from trusted, request-derived state (e.g., only accept the hint when it matches a repository that the RPC's own semantics (object pool origin/member, fork source) already establish, and validate this server-side rather than trusting caller-supplied metadata), and reject/ignore any `gitaly-partitioning-hint` value that isn't corroborated by an actual filesystem alternates relationship or a known trusted internal caller (Praefect) authenticated via a separate, non-spoofable channel.

### Proof of Concept
1. Attacker learns/guesses the relative path of a victim repository, e.g. `@hashed/aa/bb/<victim-repo-hash>.git`.
2. Attacker issues `CreateRepository` (or any repository-creation mutator RPC) directly to Gitaly, attaching outgoing gRPC metadata `gitaly-partitioning-hint: @hashed/aa/bb/<victim-repo-hash>.git` (mirroring `storage.ContextWithPartitioningHint`, which is only a `metadata.Set` call, reproducible by any gRPC client).
3. `beginTransactionForRepository` extracts the hint and sets it as `AlternateRelativePath` for the new repository's transaction.
4. `partitionAssigner.getPartitionID`/`assignPartitionID` assigns the new repository the same partition ID as the victim's repository since the new repository has no alternates file to contradict the hint.
5. The attacker's newly created repository now shares a storage partition (WAL, KV namespace) with the victim repository, which can be verified via `StorageManager.GetAssignedPartitionID` for both relative paths returning the same `storage.PartitionID`.

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

**File:** internal/gitaly/storage/storagemgr/middleware.go (L344-360)
```go
	if additionalRepo, err := methodInfo.AdditionalRepo(req); err != nil {
		if !errors.Is(err, protoregistry.ErrRepositoryFieldNotFound) {
			return transactionalizedRequest{}, fmt.Errorf("extract additional repository: %w", err)
		}

		// There was no additional repository.
	} else {
		if alternateRelativePath != "" {
			return transactionalizedRequest{}, ErrPartitioningHintAndAdditionalRepoProvided
		}

		alternateStorageName = additionalRepo.GetStorageName()
		alternateRelativePath = additionalRepo.GetRelativePath()
	}

	if alternateStorageName != "" && alternateStorageName != targetRepo.GetStorageName() {
		return transactionalizedRequest{}, ErrRepositoriesInDifferentStorages
```

**File:** internal/gitaly/storage/storagemgr/partition_manager.go (L341-363)
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
