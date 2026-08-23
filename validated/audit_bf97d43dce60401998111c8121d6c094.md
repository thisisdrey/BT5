### Title
Missing pool-membership authorization in `LinkRepositoryToObjectPool`/`CreateObjectPool` allows cross-repository object disclosure - (File: internal/gitaly/service/objectpool/link.go)

### Summary
This is the same class of bug as the Sablier report: an RPC accepts a caller-supplied "target" (there, a Lockup contract address; here, an `ObjectPool`/`origin` repository) and blindly trusts it as long as it superficially satisfies a generic shape check, without verifying that the target actually belongs to the intended relationship (there, the correct Lockup type for the campaign; here, the correct pool for the calling repository's fork network). This lets a party who can influence the `object_pool`/`origin` field redirect a repository's object resolution to an unrelated repository's data.

### Finding Description
`LinkRepositoryToObjectPool` only validates that `Repository` is a legitimate repo and that `ObjectPool` resolves to *some* valid, existing pool repository, via `poolForRequest` → `objectpool.FromProto`: [1](#0-0) 

`objectpool.FromProto` merely checks that the path matches the generic pool naming convention (`IsPoolRepository`) and that it is a valid git repository — it never checks that the pool was created from, or is otherwise associated with, the `Repository` being linked: [2](#0-1) 

Likewise, `CreateObjectPool` accepts an arbitrary `Origin` repository supplied by the caller and clones it into the caller-chosen pool location, with no check that `Origin` and the pool's intended recipients share a fork network: [3](#0-2) 

Pool relative paths are derived deterministically from a repository/project ID hash (`DerivePoolPath`/`IsRailsPoolRepository`), so they are not a secret credential; they are computable/enumerable, not a genuine access-control token: [4](#0-3) 

Once linked, Git resolves objects transparently through `objects/info/alternates` (the pool's object directory becomes part of the repository's object search path), as implemented in `internal/git/dirs.go`'s `altObjectDirs`, which only checks that the alternate directory is *within the same storage*, not that it is the *correct, authorized* alternate for the repository: [5](#0-4) 

The combination — no ownership/relationship check on `LinkRepositoryToObjectPool`/`CreateObjectPool`, plus a permissive storage-root-only check for alternates — mirrors the reported bug class: a "trusted address" field is accepted without a whitelist of legitimate counterparties, enabling a caller to substitute an unrelated, unauthorized target.

### Impact Explanation
If the `object_pool`/`origin` fields of `CreateObjectPool`/`LinkRepositoryToObjectPool` can be influenced to reference a pool or repository outside the intended fork network (e.g. through request forgery/confusion at the RPC boundary or a bug upstream that fails to bind the pool path to the correct project), an attacker's repository would gain the ability to resolve/read objects belonging to a completely unrelated (and potentially private) repository, since alternates act as a Git object search path. This constitutes cross-repository object disclosure, a serious confidentiality breach for private repositories in a multi-tenant Gitaly storage.

### Likelihood Explanation
Exploitation requires the ability to invoke `CreateObjectPool`/`LinkRepositoryToObjectPool` with attacker-chosen `object_pool`/`origin` values that do not match the legitimate pool for the given repository. Gitaly itself performs no defense-in-depth check here and relies entirely on the calling layer (normally GitLab Rails) to only ever pass matching, authorized values. Any confusion, request tampering, or logic bug in that calling layer is sufficient to trigger the disclosure, since Gitaly's own validation (`IsPoolRepository` regex + generic git-repo validity) provides no real authorization boundary.

### Recommendation
Add a positive authorization/binding check at the Gitaly RPC layer rather than relying solely on the caller: verify that the object pool was actually created from (or is otherwise cryptographically/structurally bound to) the repository being linked, e.g. by recording and checking the originating repository ID/relative path association at pool-creation time, and rejecting `LinkRepositoryToObjectPool`/`CreateObjectPool` calls whose `object_pool`/`origin` do not match the expected, verifiable relationship — analogous to the report's recommendation to whitelist only the correct contract per campaign type.

### Proof of Concept
1. Repository `A` (attacker-controlled, low privilege) and Repository `B` (victim, private) exist in the same Gitaly storage.
2. An object pool `P` was created from `B` via `CreateObjectPool{origin: B, object_pool: P}`, and `B` is or becomes a pool member.
3. Because `P`'s relative path is deterministically derived from `B`'s repository ID (`DerivePoolPath`), and `LinkRepositoryToObjectPool` performs no check that `A` is entitled to link to `P`, calling `LinkRepositoryToObjectPool{repository: A, object_pool: P}` succeeds if only generic pool-format/existence validation (`objectpool.FromProto`) is enforced.
4. After linking, `A`'s object directory search path includes `P`'s objects (`internal/git/dirs.go` `altObjectDirs`), allowing any client with access to `A` to fetch/read objects that originated from private repository `B` by OID, bypassing `B`'s access controls.

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

**File:** internal/gitaly/storage/repository_path.go (L12-58)
```go
var (
	// PraefectRootPathPrefix is the root directory for all git repositories.
	PraefectRootPathPrefix = "@cluster"
	// praefectPoolPathPrefix is the prefix directory where Praefect places object pools.
	praefectPoolPathPrefix = filepath.Join(PraefectRootPathPrefix, "pools")
	// praefectRepositoryPathPrefix is the prefix directory where Praefect places repositories.
	praefectRepositoryPathPrefix = filepath.Join(PraefectRootPathPrefix, "repositories")
	// prafectPoolDirRegexp is used to validate object pool directory structure and name as generated by Praefect.
	praefectPoolDirRegexp = regexp.MustCompile(praefectPoolPathPrefix + `/[0-9a-f]{2}/[0-9a-f]{2}/[0-9]+$`)
	// railsPoolDirRegexp is used to validate object pool directory structure and name as generated by Rails.
	railsPoolDirRegexp = regexp.MustCompile(`@pools/([0-9a-f]{2})/([0-9a-f]{2})/([0-9a-f]{64})\.git$`)
)

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

// DeriveReplicaPath derives a repository's disk storage path from its repository ID. The repository ID
// is hashed with SHA256 and the first four hex digits of the hash are used as the two subdirectories to
// ensure even distribution into subdirectories. The format is @cluster/repositories/ab/cd/<repository-id>.
func DeriveReplicaPath(repositoryID int64) string {
	return deriveDiskPath(praefectRepositoryPathPrefix, repositoryID)
}

// DerivePoolPath derives an object pools's disk storage path from its repository ID. The repository ID
// is hashed with SHA256 and the first four hex digits of the hash are used as the two subdirectories to
// ensure even distribution into subdirectories. The format is @cluster/pools/ab/cd/<repository-id>. The pools
// have a different directory prefix from other repositories so Gitaly can identify them in OptimizeRepository
// and avoid pruning them.
func DerivePoolPath(repositoryID int64) string {
	return deriveDiskPath(praefectPoolPathPrefix, repositoryID)
```

**File:** internal/git/dirs.go (L45-96)
```go
func altObjectDirs(ctx context.Context, logger log.Logger, storagePrefix, objDir string, depth int) ([]string, error) {
	const maxAlternatesDepth = 5 // Taken from https://github.com/git/git/blob/v2.23.0/sha1-file.c#L575
	if depth > maxAlternatesDepth {
		logger.WithField("objdir", objDir).WarnContext(ctx, "ignoring deeply nested alternate object directory")
		return nil, nil
	}

	fi, err := os.Stat(objDir)
	if os.IsNotExist(err) {
		logger.WithField("objdir", objDir).WarnContext(ctx, "object directory not found")
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if !fi.IsDir() {
		return nil, nil
	}

	dirs := []string{objDir}

	alternates, err := os.ReadFile(filepath.Join(objDir, "info", "alternates"))
	if os.IsNotExist(err) {
		return dirs, nil
	}
	if err != nil {
		return nil, err
	}

	for _, newDir := range strings.Split(string(alternates), "\n") {
		if len(newDir) == 0 || newDir[0] == '#' {
			continue
		}

		if !filepath.IsAbs(newDir) {
			newDir = filepath.Join(objDir, newDir)
		}

		if !strings.HasPrefix(newDir, storagePrefix) {
			return nil, alternateOutsideStorageError(newDir)
		}

		nestedDirs, err := altObjectDirs(ctx, logger, storagePrefix, newDir, depth+1)
		if err != nil {
			return nil, err
		}

		dirs = append(dirs, nestedDirs...)
	}

	return dirs, nil
}
```
