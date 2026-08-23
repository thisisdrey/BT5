### Title
Missing origin/ownership validation lets `LinkRepositoryToObjectPool` attach a repository to an unrelated object pool - (File: `internal/gitaly/service/objectpool/link.go`)

### Summary
The `VaultFactory.changeController` bug stems from mutating a critical cross-referencing field (`controller`) without verifying that the new value actually points back to the entity performing the mutation. Gitaly's `ObjectPoolService.LinkRepositoryToObjectPool` RPC has the same class of defect: it writes an object pool's path into a repository's Git alternates file without ever verifying that the specified object pool was actually created from, or is otherwise associated with, that specific repository (i.e. its true "origin"/fork-network owner).

### Finding Description
`LinkRepositoryToObjectPool` validates only that the `repository` and `object_pool` protobuf messages individually resolve to valid on-disk Git repositories: [1](#0-0) 

`s.poolForRequest` resolves the pool via `objectpool.FromProto`, which only checks that the target path is a syntactically valid pool directory (`storage.IsPoolRepository`) and that it is a well-formed Git repository (`ObjectPool.IsValid` → `locator.ValidateRepository`): [2](#0-1) [3](#0-2) 

Neither of these checks verifies any relationship ("controller"-equivalent back-reference) between the specific `repository` being linked and the specific `object_pool` supplied in the request — analogous to `VaultFactory.changeController` never confirming the new controller's `vaultFactory` field equals the calling factory (`VaultFactory.sol#L345-L359` vs. the check present in `VaultFactory.createNewMarket`, `VaultFactory.sol#L187-L190`).

The only guard that indirectly constrains this operation is the storage-manager's partition assignment logic, which requires the target repository and the additional repository (the pool) to end up in the same transaction partition: [4](#0-3) 

This is purely a transactional-consistency mechanism, not an authorization or ownership check: a repository that has not yet been assigned to any partition (e.g., a freshly created, unrelated repository) will simply be assigned into whatever partition the target pool already occupies, and the RPC succeeds. `Link` itself then performs no origin verification at all — it merely checks whether an alternates entry already exists and, if not, unconditionally writes the pool's relative object path: [5](#0-4) 

### Impact Explanation
Because Gitaly relies on caller-supplied `storage_name`/`relative_path` fields for both the `repository` and `object_pool` parameters without validating provenance, any caller able to invoke this RPC with a crafted `object_pool` field can link an arbitrary (attacker-controlled) repository to any existing object pool it can address, provided partition placement allows it (i.e., the target repository hasn't already been committed to a different partition). Once linked via `objects/info/alternates`, all objects contained in that pool — including ones only reachable through a completely unrelated project's fork network — become readable through the attacker's repository via ordinary object-lookup/`cat-file`/fetch operations. This is a cross-repository object disclosure, directly mirroring the "loss of control"/incorrect cross-linkage impact described in the source report, except here the practical consequence is object leakage rather than governance loss.

### Likelihood Explanation
Exploitation requires the ability to issue a `LinkRepositoryToObjectPool` (or equivalently `CreateObjectPool`) gRPC request with attacker-influenced `object_pool`/`repository` fields, and knowledge (or brute-forcing) of the target pool's `@pools/xx/yy/<hash>.git` relative path. Gitaly itself performs no ownership/ACL check at this layer — it is designed to trust its caller (normally GitLab Rails/Workhorse) to have already authorized the specific repository↔pool pairing. If any upstream path (crafted RPC field, replication path, or misconfigured authorization) allows an unprivileged actor to influence these two fields independently, the missing back-reference check turns that gap directly into cross-repository object access, with likelihood tied entirely to how tightly the caller layer restricts the `object_pool` field.

### Recommendation
Add an explicit ownership/provenance check analogous to `VaultFactory.createNewMarket`'s validation: when linking, verify that the object pool's recorded origin (or an authoritative mapping maintained by Gitaly/Rails, e.g., fork-network membership) actually corresponds to the `repository` being linked, rather than accepting any syntactically valid pool. At minimum, `objectpool.FromProto`/`Link` should reject linking a repository to a pool it did not originate from, instead of only checking path validity and partition co-location.

### Proof of Concept
1. Create (or identify) `object_pool_A`, an existing valid object pool belonging to project A, whose objects include private blobs/commits.
2. Create `repository_B`, a fresh, not-yet-partitioned repository controlled by the attacker.
3. Call `LinkRepositoryToObjectPool` with `Repository = repository_B` and `ObjectPool = object_pool_A`: [1](#0-0) 
4. Since neither `s.locator.ValidateRepository`, `poolForRequest`, nor `pool.Link` verify that `object_pool_A` is actually associated with `repository_B`, and `repository_B` has no prior partition assignment, the call succeeds and `repository_B`'s `objects/info/alternates` now points at `object_pool_A`'s object directory (as demonstrated generically by the "successful" case in `internal/gitaly/service/objectpool/link_test.go`, lines 133-139).
5. Any object present only in `object_pool_A` is now retrievable through `repository_B` (e.g., via `CommitService`/`cat-file` on `repository_B`), as shown by the equivalent legitimate-linking assertions in `internal/gitaly/service/objectpool/link_test.go` (lines 148-155), demonstrating that linked pool objects become fully accessible from the member repository.

### Citations

**File:** internal/gitaly/service/objectpool/link.go (L10-27)
```go
func (s *server) LinkRepositoryToObjectPool(ctx context.Context, req *gitalypb.LinkRepositoryToObjectPoolRequest) (*gitalypb.LinkRepositoryToObjectPoolResponse, error) {
	repository := req.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repository); err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}

	pool, err := s.poolForRequest(ctx, req)
	if err != nil {
		return nil, err
	}

	repo := s.localRepoFactory.Build(repository)

	if err := pool.Link(ctx, repo); err != nil {
		return nil, structerr.NewInternal("%w", err)
	}

	return &gitalypb.LinkRepositoryToObjectPoolResponse{}, nil
```

**File:** internal/git/objectpool/pool.go (L46-91)
```go
// FromProto returns an object pool object from its Protobuf representation. This function verifies
// that the object pool exists and is a valid pool repository.
func FromProto(
	ctx context.Context,
	logger log.Logger,
	locator storage.Locator,
	gitCmdFactory gitcmd.CommandFactory,
	catfileCache catfile.Cache,
	txManager transaction.Manager,
	housekeepingManager housekeepingmgr.Manager,
	proto *gitalypb.ObjectPool,
) (*ObjectPool, error) {
	poolPath, err := locator.GetRepoPath(ctx, proto.GetRepository(), storage.WithRepositoryVerificationSkipped())
	if err != nil {
		return nil, err
	}

	if !storage.IsPoolRepository(proto.GetRepository()) {
		// When creating repositories in the ObjectPool service we will first create the
		// repository in a temporary directory. So we need to check whether the path we see
		// here is in such a temporary directory and let it pass.
		tempDir, err := locator.TempDir(proto.GetRepository().GetStorageName())
		if err != nil {
			return nil, fmt.Errorf("getting temporary storage directory: %w", err)
		}

		if !strings.HasPrefix(poolPath, tempDir) {
			return nil, ErrInvalidPoolDir
		}
	}

	pool := &ObjectPool{
		Repo:                localrepo.New(logger, locator, gitCmdFactory, catfileCache, proto.GetRepository()),
		logger:              logger,
		locator:             locator,
		gitCmdFactory:       gitCmdFactory,
		txManager:           txManager,
		housekeepingManager: housekeepingManager,
	}

	if !pool.IsValid(ctx) {
		return nil, ErrInvalidPoolRepository
	}

	return pool, nil
}
```

**File:** internal/git/objectpool/pool.go (L118-121)
```go
// IsValid checks if a repository exists, and if its valid.
func (o *ObjectPool) IsValid(ctx context.Context) bool {
	return o.locator.ValidateRepository(ctx, o.Repo) == nil
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

**File:** internal/git/objectpool/link.go (L25-52)
```go
// Link will link the given repository to the object pool. This is done by writing the object pool's
// path relative to the repository into the repository's "alternates" file. This does not trigger
// deduplication, which is the responsibility of the caller.
func Link(ctx context.Context, pool, repo *localrepo.Repo, txManager transaction.Manager) (returnedErr error) {
	altPath, err := repo.InfoAlternatesPath(ctx)
	if err != nil {
		return err
	}

	expectedRelPath, err := getRelativeObjectPath(ctx, pool, repo)
	if err != nil {
		return err
	}

	linked, err := linkedToRepository(ctx, pool, repo)
	if err != nil {
		return err
	}

	if linked {
		// When the repository is already linked to the repository, cast a vote to ensure the
		// repository is consistent with the other replicas.
		if err := transaction.VoteOnContext(ctx, txManager, voting.VoteFromData([]byte("repository linked")), voting.Synchronized); err != nil {
			return fmt.Errorf("vote on linked repository: %w", err)
		}

		return nil
	}
```
