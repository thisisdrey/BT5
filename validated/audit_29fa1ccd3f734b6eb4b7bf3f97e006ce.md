### Title
`LinkRepositoryToObjectPool` accepts an arbitrary `ObjectPool` reference without verifying it is the legitimate pool for the target repository - ([File: internal/gitaly/service/objectpool/link.go])

### Summary
This finding mirrors the Escher `sale.edition` bug class: a privileged-looking operation trusts a caller-supplied reference to another entity (here, an `ObjectPool` repository) purely because it structurally "looks right" (a valid, existing Git directory that matches the pool naming convention), without verifying that this specific pool was actually the one legitimately associated (via `CreateObjectPool`) with the repository being linked. Gitaly performs no cross-check binding the `object_pool` and `repository` fields together before wiring them via the alternates mechanism.

### Finding Description
`LinkRepositoryToObjectPool` resolves the object pool purely from the untrusted request field and writes an alternates link from the caller's repository into it: [1](#0-0) 

The pool is resolved via `poolForRequest` → `objectpool.FromProto`, which only checks that the path is a *valid Git repository* and, if it isn't already lying in a temp directory, that its relative path matches the generic pool naming regex (`@pools/xx/xx/<hash>.git` or the Praefect equivalent) — it does not verify that this pool is the one actually created for/derived from the specific repository being linked: [2](#0-1) [3](#0-2) 

`ObjectPool.IsValid` only calls `locator.ValidateRepository`, which merely confirms the path resolves to an existing, well-formed Git directory — it carries no notion of "ownership" or "which source repository legitimately produced this pool": [4](#0-3) 

Once resolved, `Link` simply writes the pool's `objects` directory into the target repository's `objects/info/alternates` file, making all objects in the pool directly readable (and deduplicatable) from the target repository: [5](#0-4) 

Nothing in this call chain enforces that the `object_pool` value supplied in the `LinkRepositoryToObjectPoolRequest` is the pool that `CreateObjectPool` produced from (or was authorized against) the `repository` being linked. Any two Repository references — a repository the caller can mutate, and any other Git directory on the same storage that happens to satisfy the pool path pattern — can be paired together.

### Impact Explanation
Because Git alternates grant transparent read access to every object in the pointed-at object directory, linking a controlled repository to an arbitrary/foreign pool exposes that pool's objects (which can include blobs/commits/trees originating from other users' or projects' repositories, since pools are shared object stores for forks) to the attacker's repository. This is a cross-repository object access primitive: an attacker with legitimate mutator rights on their own repository can read (and via subsequent `GetObjectPool`/`FetchIntoObjectPool`/read RPCs, exfiltrate) objects that were never supposed to be reachable from their project, provided they can reference the target pool's storage-relative path (pool paths are content/ID-derived, but relative-path enumeration or leakage from other channels makes them discoverable).

### Likelihood Explanation
Exploitation requires only:
1. A mutator-capable RPC call the attacker already has legitimate access to (`LinkRepositoryToObjectPool` targets a repository the caller may write to).
2. Knowledge (or a guess/leak) of another pool's relative path, which follows a fixed, low-entropy directory-hashing scheme (`@pools/<2-hex>/<2-hex>/<id-or-hash>.git`), making brute-forcing across a storage plausible in shared/multi-tenant Gitaly deployments where the `object_pool` field is not otherwise constrained by an authorization layer at the Gitaly RPC boundary itself.

### Recommendation
Gitaly should not accept an arbitrary `object_pool` reference as authoritative. At minimum:
- Record the originating repository when `CreateObjectPool` creates a pool (e.g., in pool metadata) and validate at `LinkRepositoryToObjectPool` time that the given pool is actually associated with the repository being linked (or with its known ancestor/fork chain), not merely that it is *some* valid, pool-shaped Git directory.
- Alternatively, require that the caller prove derivation (e.g., only allow linking pools that were produced from a repository in the same fork network / partition as tracked internally), rather than trusting client-supplied storage-relative paths for the pool.

### Proof of Concept
1. Attacker has mutator rights on `Repository A` (their own project) and knows (or brute-forces) the relative path of `Pool P`, an object pool belonging to `Project B`'s fork network, e.g. `@pools/ab/cd/<id>.git`.
2. Attacker calls:
```
LinkRepositoryToObjectPool(
  Repository:  RepoA,
  ObjectPool:  { Repository: { StorageName: "default", RelativePath: "@pools/ab/cd/<id>.git" } },
)
```
3. `objectpool.FromProto` succeeds because `@pools/ab/cd/<id>.git` matches `IsPoolRepository` and is a valid Git directory — no check ties `Pool P` to `Repository A`. [6](#0-5) 
4. `pool.Link(ctx, repoA)` writes `Pool P`'s objects directory as an alternate into `RepoA`, making all of Project B's pool objects readable from Project A. [5](#0-4)

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

**File:** internal/gitaly/storage/repository_path.go (L25-43)
```go
// IsRailsPoolRepository returns whether the repository is a pool repository generated by Rails.
func IsRailsPoolRepository(repo Repository) bool {
	matches := railsPoolDirRegexp.FindStringSubmatch(repo.GetRelativePath())
	if matches == nil || !strings.HasPrefix(matches[3], matches[1]+matches[2]) {
		return false
	}

	return true
}

// IsPraefectPoolRepository returns whether the repository is a Praefect generated object pool repository.
func IsPraefectPoolRepository(repo Repository) bool {
	return praefectPoolDirRegexp.MatchString(repo.GetRelativePath())
}

// IsPoolRepository returns whether the repository is an object pool.
func IsPoolRepository(repo Repository) bool {
	return IsRailsPoolRepository(repo) || IsPraefectPoolRepository(repo)
}
```

**File:** internal/git/objectpool/link.go (L25-66)
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
```
