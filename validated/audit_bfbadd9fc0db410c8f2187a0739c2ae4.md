### Title
`CreateRepository`/`CreateRepositoryFromURL` can create repositories inside the reserved object-pool namespace, bypassing `CreateObjectPool`'s pool-specific gating - ([File: internal/gitaly/service/repository/create_repository.go])

### Summary
Gitaly gates the creation of object pool repositories behind the `CreateObjectPool` RPC, which explicitly checks `storage.IsPoolRepository(poolRepo)` before creating the repository via `objectpool.Create`. However, the generic `CreateRepository` (and `CreateRepositoryFromURL`) RPCs, which any client authorized to talk to the `RepositoryService` can call, create repositories through `repoutil.Create` without any check on whether the requested `RelativePath` collides with the reserved pool-directory naming convention (`@pools/xx/yy/<oid>.git` or `@cluster/pools/...`). This is directly analogous to the `MIMOProxyFactory.deployFor()` finding: a privileged/gated "factory" path (`CreateObjectPool` → `objectpool.Create`, which validates pool-shape and sets up the repository specially, e.g. without hooks) can be circumvented by calling the more general, ungated creation entrypoint (`CreateRepository`), producing repositories that live in the pool namespace but were never actually vetted/created as pools.

### Finding Description
`storage.IsPoolRepository` (in `internal/gitaly/storage/repository_path.go`, lines 26-43) recognizes a repository as a pool purely from the shape of its `RelativePath` (matching `railsPoolDirRegexp` or `praefectPoolDirRegexp`), not any persisted metadata or ownership check. Several parts of the codebase consult this predicate to change behavior for a given relative path:
- `internal/gitaly/service/objectpool/create.go` (`CreateObjectPool`) requires `storage.IsPoolRepository(poolRepo)` before allowing pool creation (`errInvalidPoolDir` otherwise), and specifically invokes `objectpool.Create`, which clones from a source repo, skips hooks (`repoutil.WithSkipInit()` + custom clone flow), and calls `FromProto`'s validity check.
- `internal/git/objectpool/pool.go`'s `FromProto` treats a path matching `IsPoolRepository` (or a temp dir) as legitimate and validates `pool.IsValid`.
- `internal/git/housekeeping/objects.go` and `internal/git/stats/repository_info.go` also branch on `IsPoolRepository` for GC/pruning special-casing (pools are exempted from normal pruning to protect deduplicated objects other repos depend on).

Critically, `internal/gitaly/service/repository/create_repository.go` (`CreateRepository`) only calls `s.locator.ValidateRepository` (generic path/storage validation) and then `repoutil.Create` — it never checks `storage.IsPoolRepository`. The same applies to `CreateRepositoryFromURL` (`internal/gitaly/service/repository/create_repository_from_url.go`), which lets a caller supply an arbitrary `RelativePath` and clone arbitrary content into it via `repoutil.Create`. Since `RelativePath` is client-supplied and only validated for path-escaping (`storage.ValidateRelativePath`) — not for whether it collides with the reserved pool-naming pattern — an ordinary caller (e.g., through Gitaly's request path used for forks/imports, or Praefect routing that trusts the relative path scheme) can create a plain repository whose `RelativePath` matches the `@pools/xx/yy/<sha256>.git` or `@cluster/pools/...` regex without ever going through `CreateObjectPool`'s gating logic.

### Impact Explanation
A repository created this way will be treated by downstream code as a legitimate object pool purely due to its path shape:
- Housekeeping/pruning logic (`internal/git/housekeeping/objects.go`) special-cases pool paths to avoid pruning objects that pool members depend on. An attacker-created "fake pool" repo would receive this exemption despite never being a real, validated pool — this can be leveraged to avoid normal repository housekeeping/pruning on a repository, effectively letting garbage/loose objects accumulate unpruned (storage abuse / partial housekeeping bypass).
- `stats/repository_info.go` similarly changes its object accounting/behavior based on `IsPoolRepository`.
- Conversely, a real `CreateObjectPool` request targeting the same derived path could collide (`AlreadyExists` from `repoutil.Create`'s pre-check at `internal/gitaly/repoutil/create.go` lines 96-104), giving a low-privilege actor a way to pre-empt/squat a legitimate pool path before the real object pool (e.g., for a fork the pool would be created for) is provisioned, causing `CreateObjectPool` to fail with `FailedPrecondition`/`AlreadyExists` — a denial-of-service against the fork/pool-creation workflow for a specific, predictable target path (pool paths for Praefect are derived deterministically from repository IDs via `DerivePoolPath`, and Rails pool paths are derived from a known SHA256 scheme), since `objectpool.Create` (`internal/git/objectpool/create.go` lines 42-48) explicitly returns `FailedPrecondition("target path exists already")` if the target exists.

This is a real but narrower analog than the original ProxyFactory bug: the object pool "registry" equivalent (the `IsPoolRepository` gate baked into `CreateObjectPool`) can be bypassed via the general repository-creation RPC, letting an ordinary caller pre-create or squat entries in the reserved pool namespace and receive inconsistent (either falsely pool-like or blocking) treatment relative to the real pool lifecycle.

### Likelihood Explanation
Likelihood is moderate: reaching `CreateRepository`/`CreateRepositoryFromURL` requires only the standard gRPC access available to any authorized Gitaly/Praefect client (the same trust level used for ordinary repository creation/import flows), and no special repository must already exist. The main constraint is predicting/computing a valid pool-shaped relative path (SHA256-derived for Rails pools, or repository-ID-derived for Praefect pools via `DerivePoolPath`), which is a public, documented, deterministic algorithm, not a secret.

### Recommendation
Add an explicit rejection in `CreateRepository` and `CreateRepositoryFromURL` (and any other generic repository-creation entrypoint using `repoutil.Create`) when the target `RelativePath` matches `storage.IsPoolRepository`, mirroring the check already present in `CreateObjectPool`. This ensures the pool namespace can only be populated through the dedicated, gated `CreateObjectPool` code path, closing the analogous "factory bypasses registry" gap.

### Proof of Concept
1. Compute a relative path matching the Rails pool-directory format: `@pools/<first-2-hex>/<next-2-hex>/<sha256>.git`, where `sha256` starts with those same 4 hex digits (satisfying `railsPoolDirRegexp` in `internal/gitaly/storage/repository_path.go` lines 21-22 and the prefix check in `IsRailsPoolRepository`).
2. Call `RepositoryService.CreateRepository` (or `CreateRepositoryFromURL`) with `Repository{StorageName: <storage>, RelativePath: <computed-pool-path>}`.
3. Observe that `CreateRepository` succeeds (`internal/gitaly/service/repository/create_repository.go`), creating a plain, non-pool repository at a path that `storage.IsPoolRepository` recognizes as a pool.
4. Subsequently call `ObjectPoolService.CreateObjectPool` with an `ObjectPool.Repository` targeting the same relative path — observe it fails with `FailedPrecondition("target path exists already")` from `internal/git/objectpool/create.go` lines 42-48, demonstrating the pool-creation workflow can be blocked/squatted by an unprivileged repository-creation call that never went through the pool-specific gate. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** internal/gitaly/service/repository/create_repository.go (L15-52)
```go
func (s *server) CreateRepository(ctx context.Context, req *gitalypb.CreateRepositoryRequest) (*gitalypb.CreateRepositoryResponse, error) {
	repository := req.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repository, storage.WithSkipRepositoryExistenceCheck()); err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}

	hash, err := git.ObjectHashByProto(req.GetObjectFormat())
	if err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}

	// When the MVCC backend is requested for new repositories, carry the MVCC reference
	// backend in the context. This selects the MVCC-enabled Git binary for every command
	// run while creating the repository (git-init as well as the subsequent for-each-ref,
	// config and repack commands) and makes git-init use the MVCC ref-format.
	if featureflag.NewRepoMVCCBackend.IsEnabled(ctx) {
		ctx = gitcmd.ContextWithReferenceBackend(ctx, git.ReferenceBackendMVCC)
	}

	if err := repoutil.Create(
		ctx,
		s.logger,
		s.locator,
		s.gitCmdFactory,
		s.catfileCache,
		s.txManager,
		s.repositoryCounter,
		repository,
		func(repo *gitalypb.Repository) error {
			// We do not want to seed the repository with any contents, so we just
			// return directly.
			return nil
		},
		repoutil.WithBranchName(string(req.GetDefaultBranch())),
		repoutil.WithObjectHash(hash),
	); err != nil {
		return nil, structerr.NewInternal("creating repository: %w", err)
	}
```

**File:** internal/gitaly/service/objectpool/create.go (L17-29)
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

**File:** internal/git/objectpool/create.go (L36-48)
```go
) (*ObjectPool, error) {
	objectPoolPath, err := locator.GetRepoPath(ctx, proto.GetRepository(), storage.WithRepositoryVerificationSkipped())
	if err != nil {
		return nil, err
	}

	if _, err := os.Stat(objectPoolPath); err == nil {
		return nil, structerr.NewFailedPrecondition("target path exists already").
			WithMetadata("object_pool_path", objectPoolPath)
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, structerr.NewInternal("checking object pool existence: %w", err).
			WithMetadata("object_pool_path", objectPoolPath)
	}
```

**File:** internal/gitaly/repoutil/create.go (L91-104)
```go
	targetPath, err := locator.GetRepoPath(ctx, repository, storage.WithRepositoryVerificationSkipped())
	if err != nil {
		return structerr.NewInvalidArgument("locate repository: %w", err)
	}

	// The repository must not exist on disk already, or otherwise we won't be able to
	// create it with atomic semantics.
	if _, err := os.Stat(targetPath); !errors.Is(err, fs.ErrNotExist) {
		if err == nil {
			return structerr.NewAlreadyExists("repository exists already")
		}

		return fmt.Errorf("pre-lock stat: %w", err)
	}
```
