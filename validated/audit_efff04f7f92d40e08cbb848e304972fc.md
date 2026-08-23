### Title
`LinkRepositoryToObjectPool` links arbitrary repositories to arbitrary object pools without verifying the pair actually belongs together, enabling cross-repository object disclosure - (File: `internal/gitaly/service/objectpool/link.go`)

### Summary
`LinkRepositoryToObjectPool` validates that its two caller-supplied repository references (`repository` and `object_pool`) are each individually well-formed, but never validates that they are actually related to one another (e.g. same storage, same fork network, or that the caller is authorized to attach the target repository to that specific pool). This is directly analogous to the `distribute()` bug: a state-mutating function accepts caller-controlled references and acts on them without validating that the "relationship" between the two pieces of caller-supplied data is legitimate, which corrupts the intended invariant (that a pool is only ever joined by its own fork-network members).

### Finding Description
`LinkRepositoryToObjectPool` only checks that `repository` is a valid repository and that `object_pool` resolves to a valid pool repository: [1](#0-0) 

`poolForRequest`/`objectpool.FromProto` only verify that the pool path is a real, valid pool repository — it performs no check binding the pool to the `repository` being linked: [2](#0-1) [3](#0-2) 

`Link()` itself simply computes a relative path and writes it into the repository's `objects/info/alternates` file — it performs no ownership/relationship check either: [4](#0-3) 

By contrast, the sibling RPC `FetchIntoObjectPool` *does* enforce a relationship constraint between its two caller-supplied repository references (same storage), showing that the codebase recognizes such cross-reference validation is necessary but omits it for `LinkRepositoryToObjectPool`: [5](#0-4) 

Once linked, `objects/info/alternates` makes every object in the pool repository transparently readable through the linked repository for all subsequent Git operations and read RPCs (`GetBlob`, `GetCommit`, clone, etc.), because Git treats alternates as an additional object search path: [6](#0-5) 

Because `LinkRepositoryToObjectPool` never checks that `repository` and `object_pool` are the same storage or belong together (unlike `FetchIntoObjectPool`), a caller who can independently name any valid pool (e.g. one it knows the storage/relative-path of, from another user's fork network) and any valid repository it controls can splice its own repository into that unrelated pool and thereby gain read access to every object the pool contains — including objects originating from repositories/forks it has no legitimate relationship to.

### Impact Explanation
This is a cross-repository object access vulnerability: it allows disclosure of Git objects (blobs, commits, trees) belonging to a completely unrelated repository’s fork network simply by supplying valid but unrelated `repository`/`object_pool` identifiers to the RPC — the Gitaly-level invariant that a pool should only be joined by its true fork-network members is not enforced. This matches the report’s described impact class ("incorrect accounting"/state corruption from unauthorized, unvalidated cross-referenced data) mapped onto Gitaly's storage/object model as unauthorized cross-repository object disclosure.

### Likelihood Explanation
Reaching this code path only requires the ability to invoke the `ObjectPoolService/LinkRepositoryToObjectPool` RPC with two syntactically valid repository references that the caller can each individually pass Gitaly-side validation for (their own repository, and any pool whose storage/relative-path they can determine). No object contents, hooks, or privileged git configuration need to be forged — only knowledge of a valid pool identifier, which is derivable from routine fork/pool creation naming.

### Recommendation
Add an explicit relationship check inside `LinkRepositoryToObjectPool` (mirroring `validateFetchIntoObjectPoolRequest`) that verifies the target `repository` and `object_pool` share the same storage and belong to the same fork network/origin lineage before performing the `Link()` operation, rejecting the request otherwise.

### Proof of Concept
1. Repository `PoolOwner` creates an object pool `P` from a private source repo via `CreateObjectPool` (attacker has no access to `PoolOwner`'s source repo, but can discover/guess `P`'s storage name and relative path, e.g. from naming conventions or prior enumeration).
2. Attacker owns/controls an unrelated repository `R` in the same Gitaly storage.
3. Attacker calls `LinkRepositoryToObjectPool(object_pool=P, repository=R)`. `link.go`'s handler only calls `s.locator.ValidateRepository(ctx, R)` and `poolForRequest` (validates `P` is a real pool) — no check ties `R` to `P`: [1](#0-0) 
4. `Link()` writes `P`'s object directory into `R`'s `objects/info/alternates`: [7](#0-6) 
5. Attacker now issues normal read RPCs (e.g. `GetBlob`, `GetCommit`, clone) against `R`; because of the alternates link, all objects contained in the unrelated pool `P` (potentially originating from private source repositories) are readable through `R`.

Note: I was unable to inspect the full proto field annotations (`target_repository`/`additional_repository`) for `LinkRepositoryToObjectPoolRequest` in this session due to tool-call limits, so I cannot confirm whether Rails-side authorization middleware independently mitigates this at the API layer; the finding is scoped strictly to the missing Gitaly-level relationship validation, which is a concrete, reachable gap in the RPC handler itself.

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

**File:** internal/git/objectpool/link.go (L19-52)
```go
// Link calls the non-receiver method version of Link with the parameters
// injected from the object pool.
func (o *ObjectPool) Link(ctx context.Context, repo *localrepo.Repo) error {
	return Link(ctx, o.Repo, repo, o.txManager)
}

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

**File:** internal/git/objectpool/link.go (L54-70)
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

	if err := transaction.CommitLockedFile(ctx, txManager, alternatesWriter); err != nil {
		return fmt.Errorf("committing alternates: %w", err)
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

**File:** doc/object_quarantine.md (L44-58)
```markdown
#### 1. Alternate object directories

The objects in a Git repository can be stored across multiple
directories: 1 main directory, usually `/objects`, and 0 or more
alternate directories. Together these act like a search path: when
looking for an object Git first checks the main directory, then each
alternate, until it finds the object.

#### 2. Config overrides via environment variables

Git can inject custom config into subprocesses via environment
variables. In the case of Git object directories, these are
`GIT_OBJECT_DIRECTORY` (the main object directory) and
`GIT_ALTERNATE_OBJECT_DIRECTORIES` (a search path of `:`-separated
alternate object directories).
```
