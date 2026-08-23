### Title
Cross-repository object disclosure via unpaired object pool `origin`/`ObjectPool` validation - (File: `internal/gitaly/service/objectpool/fetch_into_object_pool.go`)

### Summary
`FetchIntoObjectPoolRequest` and `LinkRepositoryToObjectPoolRequest` only validate that an object pool "exists" and is a syntactically valid Git directory — they never verify that the pool is actually the *correct* pool for the caller-supplied member/origin repository. This mirrors the external report's root cause: the vault-existence check (`vaultToToken(vault)` returning a non-zero `id`) was used as a stand-in for an ownership/association check, letting an attacker substitute a valid-but-unrelated vault they control. In Gitaly, `objectpool.FromProto` similarly only confirms the target path is a pool-shaped repository via `locator.ValidateRepository`/`IsValid`, with no check that this pool "belongs to" the `Origin`/target repository named in the same request.

### Finding Description
`FetchIntoObjectPool` validates the request with `validateFetchIntoObjectPoolRequest`, which only checks that `Origin` and `ObjectPool.Repository` are non-nil and reside on the same storage: [1](#0-0) 

It then resolves the pool purely from the caller-supplied `ObjectPool` field via `objectpool.FromProto`, whose only correctness checks are "does a repository exist at this path" and "is it flagged as a pool repository / in a valid pool temp dir": [2](#0-1) 

`FromProto` calls `IsValid`, which merely delegates to `locator.ValidateRepository`: [3](#0-2) 

None of these checks confirm any relationship between the named `ObjectPool` and the named `Origin`/`Repository` in the request — i.e., that the pool was actually created from, or is a legitimate alternate for, that specific repository. Any caller who can name *some* object pool they control (e.g., one they created via `CreateObjectPool` from their own project) and *any* origin repository on the same storage can direct Gitaly to fetch all objects from that origin into their own pool: [4](#0-3) 

Because pool membership deduplication works via Git alternates, and `LinkRepositoryToObjectPool` similarly does no verification that the caller-named pool is the "correct" pool for the caller-named repository (it just checks the target repo is valid and calls `pool.Link`): [5](#0-4) 

...an attacker who can link their own repository to their own pool, and separately cause that pool to fetch objects from a repository they don't fully control content of, ends up with all of that origin's objects readable through their own linked repository — exactly analogous to the report's pattern of "exists → treated as authorized," rather than "exists AND is the one legitimately associated with this actor's own vault/repo."

### Impact Explanation
If Rails-side or Praefect-side authorization does not independently and strictly enforce that the `Origin` in `FetchIntoObjectPoolRequest` is a repository the caller is entitled to read (and is the pool's legitimate primary member), this flow allows exfiltration of Git objects (commits, blobs, trees) belonging to another repository/project into an attacker-controlled object pool, and from there into an attacker-controlled repository — i.e., a cross-repository object disclosure. This is a data confidentiality issue analogous to unauthorized access to another party's asset via a superficially "valid" but unrelated container object (the vault in the original report; the object pool here).

### Likelihood Explanation
Exploitability hinges entirely on whether the calling layer (GitLab Rails / Praefect) enforces that `Origin` and `ObjectPool` in the RPC are tied to the same fork network / project pairing before invoking Gitaly. Gitaly itself, at the RPC-handler level examined here, performs no such pairing check — the storage-name equality check in `validateFetchIntoObjectPoolRequest` is the only cross-field validation present. Because Gitaly is designed to trust its callers for authorization and only defends at the storage/path level, this is a real gap in Gitaly's own validation, but whether it is independently reachable by an "ordinary user" depends on Rails-side checks that are out of scope for this repository. This uncertainty means the likelihood cannot be fully confirmed from the Gitaly codebase alone.

### Recommendation
Add an explicit association check in `objectpool.FromProto`/`FromRepo` or at the RPC layer (`FetchIntoObjectPool`, `LinkRepositoryToObjectPool`) that verifies the named object pool is actually linked to (or was created from) the named origin/target repository — e.g., by checking that the target repository's `objects/info/alternates` already resolves to this pool (as `linkedToRepository` already does for re-linking) before permitting a fetch, or by requiring a signed/derived relationship (such as the pool's relative path being deterministically derived from the origin's identity) rather than accepting arbitrary caller-supplied pairings at face value.

### Proof of Concept
1. Attacker creates their own repository `attacker-repo` and pool `attacker-pool` via `CreateObjectPool` (legitimately, from their own project) — `objectpool.FromProto`/`IsValid` only checks the pool exists and looks like a pool, per `internal/git/objectpool/pool.go` lines 46-91 and 118-121.
2. Attacker links `attacker-repo` to `attacker-pool` via `LinkRepositoryToObjectPool`, which contains no check tying the pool to any particular origin beyond storage-name matching (`internal/gitaly/service/objectpool/link.go` lines 10-28).
3. Attacker calls `FetchIntoObjectPool` with `Origin` set to `victim-repo` (any repository on the same storage) and `ObjectPool` set to `attacker-pool`. `validateFetchIntoObjectPoolRequest` only checks storage-name equality (`internal/gitaly/service/objectpool/fetch_into_object_pool.go` lines 102-117), so this request is accepted and Gitaly fetches all of `victim-repo`'s objects into `attacker-pool` (lines 16-32 of the same file).
4. Because `attacker-repo` is an alternate of `attacker-pool`, the attacker can now read `victim-repo`'s objects (commits, blobs, trees) through their own `attacker-repo`, achieving unauthorized cross-repository object disclosure — analogous to the attacker retrieving the NFT via a superficially valid but unassociated vault in the original report.

### Citations

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

**File:** internal/gitaly/service/objectpool/fetch_into_object_pool.go (L102-117)
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
