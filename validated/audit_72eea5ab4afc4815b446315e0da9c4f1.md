### Title
Missing Ownership Validation Allows Linking a Repository to an Arbitrary Object Pool, Enabling Cross-Repository Object Disclosure - (`internal/gitaly/service/objectpool/link.go`)

### Summary
The `LinkRepositoryToObjectPool` RPC only validates that the target repository and the object pool are individually well-formed, but never verifies that the caller-supplied object pool actually belongs to the same fork network as the target repository. This mirrors the `SHToken.burn()` bug class: an operation that mutates/exposes one entity's state is executed on a caller-supplied target without checking that the target legitimately "belongs" to the requester's own resource.

### Finding Description
`LinkRepositoryToObjectPool` takes a `Repository` and an `ObjectPool`, both fully attacker-controlled fields in the request (`object_pool` is only marked `additional_repository`, not verified against the target repository's actual fork/pool relationship): [1](#0-0) 

The handler validates only that the repository is well-formed and that the pool resolves to a valid pool repository — it never checks that `pool` is the object pool that `repository` was actually forked from or is otherwise entitled to join: [2](#0-1) 

`poolForRequest` / `objectpool.FromProto` only check that the given repository path is a *valid pool repository* (exists on disk, marked as a pool, passes `IsValid`) — there is no cross-check against the target repository's origin/fork network: [3](#0-2) [4](#0-3) 

`objectpool.Link` then unconditionally writes the given pool's relative path into `objects/info/alternates` of the target repository: [5](#0-4) 

Once linked, Git treats the pool's entire object database as directly readable from the linking repository (that is the whole point of alternates/object pools), as documented: [6](#0-5) 

The only structural constraint enforced elsewhere is that pool and repository share the same storage/partition (a partitioning/co-location concern, not an authorization concern): [7](#0-6) 

None of this constitutes an ownership check: any caller able to invoke `LinkRepositoryToObjectPool` for a repository they control can point it at *any* existing object pool on the same storage, including one seeded from a different, unrelated (and potentially private) project.

### Impact Explanation
Successful exploitation grants full read access to all Git objects (blobs, trees, commits — i.e., full source code and history) contained in an object pool that does not belong to the attacker's project. Since `FetchIntoObjectPool`/`GetObjectPool`/`CommitService` will happily serve the now-shared objects to anyone with read access to the linking repository, this is a concrete cross-repository object access / confidentiality violation, directly analogous to the reported bug class where a caller-supplied target lets an unprivileged user affect/read data belonging to someone else without proof of relationship.

### Likelihood Explanation
Any actor capable of issuing an authenticated gRPC `LinkRepositoryToObjectPool` request (which in production is gated by GitLab Rails' authorization layer, but is a raw, unauthenticated-at-Gitaly-layer operation acting only on caller-supplied identifiers) can trigger this as long as they know or can guess/enumerate the relative path of an existing pool repository (pool paths follow a predictable `@pools/xx/yy/<oid>` scheme). No git push/fetch content manipulation is required — only two `Repository`/`ObjectPool` messages.

### Recommendation
Before linking, verify that the object pool and the target repository share the same fork/pool network — e.g., by checking that the pool was created from (or that the repository is a legitimate member candidate of) that specific pool, using the pool's recorded member list or the repository's storage-level linkage recorded at `CreateObjectPool`/`CreateFork` time, rather than trusting the caller-supplied `ObjectPool` message at face value.

### Proof of Concept
1. Project A's forked repository `repoA` has been linked to pool `poolA` (created from `repoA`'s source).
2. An attacker who controls `repoB` (unrelated project, but on the same Gitaly storage) issues:
```
LinkRepositoryToObjectPool(
  Repository: repoB,
  ObjectPool: { Repository: poolA },  // pool belonging to Project A, not B
)
```
3. `internal/gitaly/service/objectpool/link.go`'s handler validates only that `repoB` is well-formed and that `poolA` is a valid pool repository, then calls `objectpool.Link`, which writes `poolA`'s path into `repoB/objects/info/alternates`.
4. Subsequent `CommitService`/`RefService` calls against `repoB` can now resolve and read any object stored in `poolA`, including objects only reachable from Project A's private repositories that feed that pool.

### Citations

**File:** proto/objectpool.proto (L123-129)
```text
// LinkRepositoryToObjectPoolRequest is a request for the LinkRepositoryToObjectPool RPC.
message LinkRepositoryToObjectPoolRequest {
  // object_pool is the object pool to which the repository shall be linked to.
  ObjectPool object_pool = 1 [(additional_repository)=true];
  // repository is the repository that shall be linked to the object pool.
  Repository repository = 2 [(target_repository)=true];
}
```

**File:** internal/gitaly/service/objectpool/link.go (L10-28)
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
}
```

**File:** internal/gitaly/service/objectpool/util.go (L35-50)
```go
func (s *server) poolForRequest(ctx context.Context, req PoolRequest) (*objectpool.ObjectPool, error) {
	pool, err := objectpool.FromProto(ctx, s.logger, s.locator, s.gitCmdFactory, s.catfileCache, s.txManager, s.housekeepingManager, req.GetObjectPool())
	if err != nil {
		if errors.Is(err, objectpool.ErrInvalidPoolDir) {
			return nil, errInvalidPoolDir
		}

		if errors.Is(err, objectpool.ErrInvalidPoolRepository) {
			return nil, structerr.NewFailedPrecondition("%w", err)
		}

		return nil, structerr.NewInternal("%w", err)
	}

	return pool, nil
}
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

**File:** internal/git/objectpool/link.go (L28-66)
```go
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

**File:** doc/object_pools.md (L10-19)
```markdown
The sharing of objects for a given repository and its object pool is done via
alternate object directories which Gitaly sets up when linking a repository to
an object pool by writing the `objects/info/alternates` file.

## Lifetime of Object Pools

The lifetime of object pools is maintained via the
[ObjectPoolService](../proto/objectpool.proto), which provides various RPCs to
create and delete object pools as well as to add members to or remove members
from the pool.
```

**File:** internal/gitaly/storage/storagemgr/middleware.go (L332-360)
```go
	// Object pools need to be placed in the same partition as their members. Below we figure out which repository,
	// if any, the target repository of the RPC must be partitioned with. We figure this out using two strategies:
	//
	// The general case is handled by extracting the additional repository from the RPC, and partitioning the target
	// repository of the RPC with the additional repository. Many of the ObjectPoolService's RPCs operate on two
	// repositories. Depending on the RPC, the additional repository is either the object pool itself or a member
	// of the pool.
	//
	// CreateFork is special cased. The fork must partitioned with the source repository in order to successfully
	// link it with the object pool later. The source repository is not tagged as additional repository in the
	// CreateForkRequest. If the request is CreateForkRequest, we extract the source repository and partition the
	// fork with it.
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
