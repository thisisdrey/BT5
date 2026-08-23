### Title
Missing authorization on object pool identity in `LinkRepositoryToObjectPool` allows cross-repository object disclosure - (File: internal/gitaly/service/objectpool/link.go)

### Summary
`LinkRepositoryToObjectPool` accepts an arbitrary `ObjectPool` repository reference in the request and links it as a Git alternate for the caller-specified target repository, with no check that the target repository is actually a legitimate member/owner of that pool. This is the same missing-access-control pattern as the `DcntEth::setRouter` bug: a security-sensitive relationship (which Router is trusted / which pool a repo is linked to) is set purely from caller-supplied data, with the only "authorization" performed by an upstream, untrusted caller layer.

### Finding Description
The RPC handler only validates that the target repository is well-formed and that the pool repository resolves to a valid pool on disk; it never checks that the requesting repository is entitled to join that particular pool: [1](#0-0) 

`poolForRequest` simply resolves whatever `ObjectPool` proto was supplied in the request via `objectpool.FromProto`, which only verifies the pool directory looks like a pool repository (`storage.IsPoolRepository`) and that it is `IsValid` (has the pool's own alternates/config layout) — it performs no ownership/ACL check tying the pool to the specific member repository being linked: [2](#0-1) [3](#0-2) 

`Link()` then unconditionally writes the pool's object directory into the target repository's `objects/info/alternates` file: [4](#0-3) 

The only constraint enforced elsewhere is that the pool and the target repository must reside in the *same storage* (partitioning requirement), not that they belong to the same project/fork network: [5](#0-4) 

Exactly like `DcntEth::setRouter()` lacking `onlyOwner`/access control before trusting an attacker-supplied address, `LinkRepositoryToObjectPool` lacks any check that the caller-supplied `ObjectPool` reference is the pool that the target repository is actually supposed to belong to (e.g. the pool of its fork network). Gitaly relies entirely on the calling application (e.g. GitLab Rails) to have performed that authorization before issuing the RPC — if that check is missing, bypassed, or the RPC is reachable via a lower-trust path (e.g. Praefect replication, or any client holding a valid Gitaly auth token but without per-project authorization), a repository the attacker controls can be linked to any other pool repository on the same storage.

### Impact Explanation
Once the attacker's own repository is linked as a member of an arbitrary pool, all objects contained in that pool (which normally are the deduplicated objects of the pool's legitimate owner, e.g. a private upstream project) become readable through Git operations against the attacker's own repository (the alternate object store is used transparently for object lookups, e.g. via `cat-file`, fetch/clone of "unreachable" objects, etc.). This is a cross-repository object disclosure — the pool's contents leak into a repository the attacker fully controls, without any project-level or namespace-level authorization performed by Gitaly itself, mirroring the "gain access to functions restricted for the router" impact in the original report (unauthorized access to a security-critical binding).

### Likelihood Explanation
Reachability requires an authenticated Gitaly client capable of invoking `ObjectPoolService.LinkRepositoryToObjectPool` (a `MUTATOR` RPC) with an arbitrary `ObjectPool` field — this is a normal, unprivileged-at-the-Gitaly-layer RPC surface (the same trust boundary as ordinary push/fork RPCs), and Gitaly itself performs zero pool-membership authorization, delegating this entirely to the caller. Any defect or gap in the calling application's authorization logic (or any RPC caller other than the intended one, e.g. replication/administration tooling) results in immediate exploitation with no further validation inside Gitaly.

### Recommendation
Add an explicit relationship check in `poolForRequest`/`LinkRepositoryToObjectPool` (and the analogous `FetchIntoObjectPool`/`DisconnectGitAlternates` paths) verifying that the target repository is a legitimate, previously-registered member of the specified pool (e.g. via a stored pool-membership mapping) rather than trusting the pool reference supplied in the request. At minimum, do not allow linking a repository to a pool it was not created from (`CreateObjectPool`/`CreateFork`), and require that this membership be recorded server-side rather than derived solely from caller-supplied protobuf fields.

### Proof of Concept
1. Attacker creates/owns Repository A on the same Gitaly storage as a private Repository B's object pool P (pool relative paths are deterministic/discoverable, e.g. `@pools/<hash>`).
2. Attacker calls `ObjectPoolService.LinkRepositoryToObjectPool` with `Repository = A` and `ObjectPool = {Repository: P}`.
3. `poolForRequest` (internal/gitaly/service/objectpool/util.go:35-49) resolves P successfully since it is a structurally valid pool.
4. `pool.Link(ctx, A)` (internal/git/objectpool/link.go:28-66) writes P's object directory into A's `objects/info/alternates`.
5. Attacker performs `git cat-file`/fetch operations against A and can now read any object contained in pool P, including objects only reachable from private Repository B.

### Citations

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

**File:** internal/gitaly/service/objectpool/util.go (L35-49)
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
```

**File:** internal/git/objectpool/pool.go (L46-75)
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

**File:** internal/gitaly/storage/storagemgr/middleware.go (L332-361)
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
	}
```
