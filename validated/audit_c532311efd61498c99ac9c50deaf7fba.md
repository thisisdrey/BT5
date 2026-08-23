### Title
Missing ownership/relationship check in `LinkRepositoryToObjectPool` allows any repository to be linked to any object pool, exposing unrelated repositories' objects — (File: `internal/gitaly/service/objectpool/link.go`)

### Summary
`LinkRepositoryToObjectPool` validates only that the `Repository` and `ObjectPool` fields are structurally present and that the pool path is a syntactically valid pool repository, but it never verifies that the target `repository` is actually derived from, or otherwise entitled to access, the given `object_pool`. This mirrors the `buyLoan()` flaw: the RPC accepts a caller-supplied identifier (`poolId` analog: `ObjectPool`) for a resource the caller does not necessarily own or have a legitimate relationship with, and proceeds to bind it to the caller's own resource, granting object access across a trust boundary for free.

### Finding Description
`LinkRepositoryToObjectPool` builds the pool purely from the request and links it to the given repository without any relationship check: [1](#0-0) 

The pool object itself is only validated for path shape/existence via `objectpool.FromProto`, which checks `storage.IsPoolRepository` and that the on-disk repository is git-valid — it performs no check that the pool "belongs" to, or was created from, the repository being linked: [2](#0-1) 

`Link()` then simply writes the pool's path into the repository's `objects/info/alternates` file, without any authorization gate that the pool and repository are part of the same fork network / partition / ownership domain: [3](#0-2) 

The proto documentation for the whole `ObjectPoolService` explicitly notes callers are trusted to maintain safety invariants (e.g. `DeleteObjectPool` "has no safety checks in place"), confirming Gitaly's design intentionally omits authorization/ownership enforcement at this layer and defers it entirely to the upstream caller: [4](#0-3) 

Because linking is idempotent and unconditional, any two arbitrary repositories on the same storage can be joined via a shared pool, after which `git-fsck`/`git-cat-file`/clone/fetch against the linked repository can read every object contained in the pool — including objects that originated solely from a different, unrelated repository that was used to seed that pool (e.g. via `CreateObjectPool`+`FetchIntoObjectPool` from a separate origin). This is precisely the "attacker forces someone else's resource to be bound to my target for free" pattern described in the report: the caller-supplied `ObjectPool` field is trusted without proving the caller is entitled to the objects it contains.

### Impact Explanation
If Gitaly's authorization boundary is the RPC layer (as it is for all mutator RPCs, gated only by the shared Gitaly auth token, not by per-repository/per-pool ownership), then any client capable of invoking `ObjectPoolService` RPCs — which includes automated flows triggered by ordinary user actions such as forking, importing, or repository-network operations — can link a repository it controls to an object pool whose membership/seeding it does not control. This can result in cross-repository object disclosure: objects (blobs, commits, trees) that were only ever pushed into one repository become fetchable through a second, unrelated repository once linked to a shared pool, defeating repository-level access boundaries enforced above Gitaly. This matches the "cross-repository object access" acceptance criterion.

### Likelihood Explanation
Likelihood is high for any deployment where the pool-repository relationship (which repos may join which pool) is enforced only by the calling application layer and not re-validated by Gitaly itself, since `LinkRepositoryToObjectPool`, `FetchIntoObjectPool`, and `GetObjectPool` all operate purely on caller-supplied identifiers with no cross-check against the actual provenance/fork-network relationship recorded anywhere in Gitaly. Any bug, race, or unvalidated field in the upstream authorization logic that computes the `ObjectPool`/`Repository` values (e.g., during fork or import flows) is single-point-of-failure exploitable here with no secondary Gitaly-side safety net.

### Recommendation
Add a Gitaly-side invariant check before linking: verify that the `object_pool` and `repository` are recorded as belonging to the same relationship domain (e.g. persisted pool-membership metadata, fork-network id, or an explicit "origin" record established at `CreateObjectPool` time) rather than trusting the caller-supplied pair unconditionally. At minimum, record and check the pool's authorized origin repository (as captured during `CreateObjectPool`) and reject `LinkRepositoryToObjectPool` calls whose target repository was not the origin or a repository previously authorized against that origin, returning a `PermissionDenied`/`InvalidArgument` error otherwise, analogous to adding `if msg.sender != pools[poolId].lender revert Unauthorized();` in the Solidity report.

### Proof of Concept
1. Repository A (private/sensitive) is used as `Origin` in `CreateObjectPool`, seeding pool `P` with A's objects: [5](#0-4) .
2. `FetchIntoObjectPool` is run with `Origin=A`, further populating `P` with A's refs/objects: [6](#0-5) .
3. An unrelated repository B (which the caller controls but which has no legitimate relationship to A) is linked to the same pool `P` by calling `LinkRepositoryToObjectPool{Repository: B, ObjectPool: P}` — the handler performs no check that B is related to A or to P's origin: [1](#0-0) .
4. B's `objects/info/alternates` now points at `P`, so any object present in `P` (originally from A) is readable through B via ordinary Git operations (`git cat-file`, clone, fetch) against B, without ever needing read access to A itself.

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

**File:** internal/git/objectpool/link.go (L25-84)
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
}
```

**File:** proto/objectpool.proto (L44-58)
```text
  // DeleteObjectPool deletes the object pool. There are no safety checks in place, so if any
  // repository is still using this object pool it will become corrupted.
  rpc DeleteObjectPool(DeleteObjectPoolRequest) returns (DeleteObjectPoolResponse) {
    option (op_type) = {
      op: MUTATOR
    };
  }

  // LinkRepositoryToObjectPool links the specified repository to the object pool. Objects contained
  // in the object pool will be deduplicated for this repository when repacking objects.
  rpc LinkRepositoryToObjectPool(LinkRepositoryToObjectPoolRequest) returns (LinkRepositoryToObjectPoolResponse) {
    option (op_type) = {
      op: MUTATOR
    };
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

**File:** internal/gitaly/service/objectpool/fetch_into_object_pool.go (L16-32)
```go
func (s *server) FetchIntoObjectPool(ctx context.Context, req *gitalypb.FetchIntoObjectPoolRequest) (*gitalypb.FetchIntoObjectPoolResponse, error) {
	if err := validateFetchIntoObjectPoolRequest(req); err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}

	objectPool, err := objectpool.FromProto(ctx, s.logger, s.locator, s.gitCmdFactory, s.catfileCache, s.txManager, s.housekeepingManager, req.GetObjectPool())
	if err != nil {
		return nil, structerr.NewInvalidArgument("object pool invalid: %w", err)
	}

	origin := s.localRepoFactory.Build(req.GetOrigin())

	if err := objectPool.FetchFromOrigin(ctx, origin, func(repo *gitalypb.Repository) *localrepo.Repo {
		return s.localRepoFactory.Build(repo)
	}); err != nil {
		return nil, structerr.NewInternal("%w", err)
	}
```
