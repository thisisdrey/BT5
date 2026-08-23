Based on my investigation, I found a genuine analog to the "missing identity binding in verification" bug class in Gitaly's object pool linking logic.

### Title
`LinkRepositoryToObjectPool` does not verify pool/repository ownership, allowing cross-repository object pool linking - ([File: internal/gitaly/service/objectpool/link.go](internal/gitaly/service/objectpool/link.go))

### Summary
Just as the Tendermint light client validated cryptographic and quorum conditions on a header but omitted checking that the header's chain ID matched the trusted chain (allowing headers from a foreign but structurally-valid chain to be accepted), Gitaly's `LinkRepositoryToObjectPool` RPC validates that a repository and an object pool each independently exist and are structurally valid Git repositories, but never verifies that the two belong to the same logical unit (fork network / project). Any storage-valid pool can be linked to any storage-valid repository.

### Finding Description
`LinkRepositoryToObjectPool` [1](#0-0)  only validates the repository via `s.locator.ValidateRepository` and resolves the pool via `poolForRequest`, which in turn calls `objectpool.FromProto` [2](#0-1) . `FromProto` only checks that the pool path exists, is within the pools directory (`storage.IsPoolRepository`) or a temp dir, and `pool.IsValid` (i.e., a valid Git repository) [3](#0-2) . Nowhere is there a check that the `origin`/target repository that requested pool creation (`GlProjectPath`, `GlRepository`, or fork-network identity) matches the identity encoded when the pool was created via `CreateObjectPool` [4](#0-3) .

The actual `Link` operation then writes the pool's relative path into the target repository's Git alternates file unconditionally as long as it isn't already linked to a *different* pool [5](#0-4)  and [6](#0-5) . There is no cross-check analogous to a "chain ID" — e.g., no verification that the pool's origin repository's `GlProjectPath`/fork-network ID matches the repository being linked. The only identity constraint enforced is storage-name equality, seen in the sibling `FetchIntoObjectPool` validator [7](#0-6) , but `LinkRepositoryToObjectPool` doesn't even enforce that much of a check beyond what's baked into `objectpool.FromProto`.

### Impact Explanation
Since Git alternates grant read access to every object in the alternate (pool) repository, linking an arbitrary repository to an object pool that was created from a different, unrelated project would expose all of that unrelated project's Git objects (commits, blobs, trees) to any client that can subsequently read the linked repository — a cross-repository object disclosure. This is analogous to the light-client accepting signatures from the wrong chain: the individual structural checks pass, but the "identity" binding between the two verified entities is missing.

### Likelihood Explanation
Exploitability depends on what actor can invoke `LinkRepositoryToObjectPool`/`CreateObjectPool` — these are typically restricted to GitLab Rails/internal callers via authenticated gRPC token, not to ordinary unauthenticated push/fetch users. I was not able to conclusively verify, within the scope of this investigation, whether any ordinary user-triggered flow (e.g. project fork creation) can supply an attacker-controlled object pool that does not belong to their own fork network, since that binding logic likely lives in the calling application (GitLab Rails) rather than in Gitaly itself. Gitaly's `link.go` and `objectpool/pool.go` show no code-level constraint preventing it, but whether it's reachable by an "ordinary user" per the report's scoping rules is uncertain from the Gitaly codebase alone.

### Recommendation
Add an explicit identity check in `LinkRepositoryToObjectPool` (or in `objectpool.FromProto`/`Link`) that verifies the pool's associated project/fork-network identity (e.g. `GlProjectPath` lineage recorded at `CreateObjectPool` time) matches the repository being linked, rather than only verifying storage-level path validity.

### Proof of Concept
Not constructed — reachability from an unprivileged/ordinary-user-triggered RPC path could not be confirmed with the available code index; the mismatch was identified at the code level (`link.go`/`pool.go`) but the calling context that determines "ordinary user" reachability sits outside this repository's visible code (GitLab Rails internal API), so a concrete unprivileged PoC could not be validated here.

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

**File:** internal/gitaly/service/objectpool/create.go (L17-61)
```go
func (s *server) CreateObjectPool(ctx context.Context, in *gitalypb.CreateObjectPoolRequest) (*gitalypb.CreateObjectPoolResponse, error) {
	if in.GetOrigin() == nil {
		return nil, errMissingOriginRepository
	}

	poolRepo := in.GetObjectPool().GetRepository()
	if poolRepo == nil {
		return nil, errMissingPool
	}

	if !storage.IsPoolRepository(poolRepo) {
		return nil, errInvalidPoolDir
	}

	// repoutil.Create creates the repositories in a temporary directory. This means the repository is not created in the location
	// expected by the transaction manager. This makes sense without transactions, but with transactions, there's no real point in
	// doing so given a failed transaction's state is anyway removed. Creating the repository in a temporary directory is problematic
	// as the reference transaction hook is invoked for the repository from unexpected location, causing the transaction to fail to
	// associate the reference updates with the repository.
	//
	// Run the repository creation without the transaction in the context. The transactions reads the created repository's state from
	// the disk when committing it, so it's not necessary to capture the updates from the reference-transaction hook. This avoids the
	// problem for now, and later with transactions enabled by default we can stop creating repositories in unexpected locations.
	ctxWithoutTransaction := storage.ContextWithTransactionID(ctx, 0)
	if err := repoutil.Create(ctxWithoutTransaction, s.logger, s.locator, s.gitCmdFactory, s.catfileCache, s.txManager, s.repositoryCounter, poolRepo, func(poolRepo *gitalypb.Repository) error {
		if _, err := objectpool.Create(
			ctxWithoutTransaction,
			s.logger,
			s.locator,
			s.gitCmdFactory,
			s.catfileCache,
			s.txManager,
			s.housekeepingManager,
			&gitalypb.ObjectPool{
				Repository: poolRepo,
			},
			s.localRepoFactory.Build(in.GetOrigin()),
		); err != nil {
			return err
		}

		return nil
	}, repoutil.WithSkipInit()); err != nil {
		return nil, structerr.New("creating object pool: %w", err)
	}
```

**File:** internal/git/objectpool/link.go (L28-83)
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

	if err := transaction.CommitLockedFile(ctx, txManager, alternatesWriter); err != nil {
		return fmt.Errorf("committing alternates: %w", err)
	}

	if tx := storage.ExtractTransaction(ctx); tx != nil {
		alternatesRelativePath, err := filepath.Rel(tx.FS().Root(), altPath)
		if err != nil {
			return fmt.Errorf("rel alternates file: %w", err)
		}

		if err := tx.FS().RecordFile(alternatesRelativePath); err != nil {
			return fmt.Errorf("record alternates file")
		}
	}

	return removeMemberBitmaps(ctx, pool, repo)
```

**File:** internal/git/objectpool/link.go (L168-204)
```go
// linkedToRepository tests if a repository is linked to an object pool
func linkedToRepository(ctx context.Context, pool, repo *localrepo.Repo) (bool, error) {
	poolPath, err := pool.Path(ctx)
	if err != nil {
		return false, fmt.Errorf("getting object pool path: %w", err)
	}

	repoPath, err := repo.Path(ctx)
	if err != nil {
		return false, fmt.Errorf("getting repo path: %w", err)
	}

	altInfo, err := stats.AlternatesInfoForRepository(repoPath)
	if err != nil {
		return false, fmt.Errorf("getting alternates info: %w", err)
	}

	if !altInfo.Exists || len(altInfo.ObjectDirectories) == 0 {
		return false, nil
	}

	relPath := altInfo.ObjectDirectories[0]
	expectedRelPath, err := getRelativeObjectPath(ctx, pool, repo)
	if err != nil {
		return false, err
	}

	if relPath == expectedRelPath {
		return true, nil
	}

	if filepath.Clean(relPath) != filepath.Join(poolPath, "objects") {
		return false, fmt.Errorf("unexpected alternates content: %q", relPath)
	}

	return false, nil
}
```

**File:** internal/gitaly/service/objectpool/fetch_into_object_pool.go (L102-118)
```go
func validateFetchIntoObjectPoolRequest(req *gitalypb.FetchIntoObjectPoolRequest) error {
	if req.GetOrigin() == nil {
		return errors.New("origin is empty")
	}

	if req.GetObjectPool() == nil {
		return errors.New("object pool is empty")
	}

	originRepository, poolRepository := req.GetOrigin(), req.GetObjectPool().GetRepository()

	if originRepository.GetStorageName() != poolRepository.GetStorageName() {
		return errors.New("origin has different storage than object pool")
	}

	return nil
}
```
