### Title
Gitaly's `LinkRepositoryToObjectPool` trusts a caller-supplied `ObjectPool` reference without verifying it actually belongs to the requesting repository, enabling cross-repository object disclosure - ([File: internal/gitaly/service/objectpool/link.go])

### Summary
`LinkRepositoryToObjectPool` accepts two independent, caller-controlled `Repository` messages in the same RPC: the `Repository` to be linked and the `ObjectPool` to link it to. Gitaly validates each side structurally (is it a valid git directory, is it "pool-shaped") but never validates that the two are actually related — i.e., that the pool is the pool that legitimately belongs to the target repository's fork network/project. This mirrors the GnosisSafeRegistry flaw: the registry checked structural properties of a submitted wallet (single owner, threshold 1) but never verified the *relationship* between the claimed owner and the actual controller, letting an attacker submit structurally-valid but illegitimately-associated data.

### Finding Description
The RPC handler only checks that the target repository is valid and then resolves the pool via `poolForRequest`/`objectpool.FromProto`: [1](#0-0) 

`FromProto` verifies the pool path is either recognizably pool-shaped (`storage.IsPoolRepository`) or sits in the storage's temp directory, and that it's a structurally valid pool repository: [2](#0-1) 

Nowhere in this chain is there a check that the `ObjectPool` argument is the pool that the `Repository` argument is actually entitled to use (e.g., that it belongs to the same fork network / project). `Link` then unconditionally writes the pool's relative object path into the target repository's `objects/info/alternates` file: [3](#0-2) 

Because pool paths are deterministically derived from a repository ID (`DerivePoolPath`, a SHA-256-prefixed, sequential-ID-based path) rather than from an unguessable secret, an ordinary user who can trigger this RPC against a repository they control (e.g., their own project/fork) can supply an `ObjectPool` value pointing at another project's pool repository: [4](#0-3) 

Once linked, `git` commands run against the attacker's own repository transparently gain the ability to read any object present in the victim pool's object database via the alternates mechanism, since alternates grant read access to all objects in the pointed-to object store without further authorization checks at the Git layer.

### Impact Explanation
An attacker who controls only their own repository can, by supplying a foreign but structurally-valid pool path in the `ObjectPool` field, cause Gitaly to link their repository to another project's pool. This exposes objects (blobs, commits, trees — including private repository content that has been deduplicated into a pool) that the attacker's project has no authorization to access — a cross-repository object access/information disclosure comparable to the "backdoored wallet" scenario, where structurally-compliant but unauthorized associations bypass the intended trust boundary.

### Likelihood Explanation
Exploitability depends on whether higher layers (Rails/GitLab) also independently verify the pool-to-repository relationship before issuing the Gitaly RPC, and on the difficulty of guessing/enumerating another project's pool path (`DerivePoolPath` is deterministic from a repository ID, which is often sequential and can be inferred). At the Gitaly layer alone, there is no defense against a mismatched pool given a raw RPC request, so likelihood is moderate-to-high if any client path allows a user to influence or supply the pool identifier independently of server-side derivation from the target repository.

### Recommendation
When linking a repository to a pool, Gitaly should independently derive/verify the expected pool for the given repository (e.g., re-derive `DerivePoolPath` from the repository's own project/fork-network identity server-side) rather than trusting the caller-supplied `ObjectPool.Repository` value, or otherwise cryptographically/structurally bind pool and member repository (e.g., via a stored, unforgeable pool-to-repository mapping checked before `Link` proceeds).

### Proof of Concept
1. Attacker owns/administers `RepoA` (any repository they can call Gitaly RPCs against).
2. Attacker learns or derives `DerivePoolPath(victimRepositoryID)` for `RepoB`'s pool (deterministic hash of a numeric ID).
3. Attacker issues `LinkRepositoryToObjectPool{ Repository: RepoA, ObjectPool: { StorageName: same, RelativePath: derived victim pool path } }`.
4. `FromProto` accepts the pool because it matches the pool-shape regex and is a valid git repository; `Link` writes the victim pool's `objects` directory into `RepoA`'s `alternates` file.
5. Attacker runs any `git` read command (e.g., `git cat-file`, `git log`) against `RepoA`; objects from the victim's pool are now resolvable, disclosing content the attacker was never authorized to read.

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

**File:** internal/gitaly/storage/repository_path.go (L52-59)
```go
// DerivePoolPath derives an object pools's disk storage path from its repository ID. The repository ID
// is hashed with SHA256 and the first four hex digits of the hash are used as the two subdirectories to
// ensure even distribution into subdirectories. The format is @cluster/pools/ab/cd/<repository-id>. The pools
// have a different directory prefix from other repositories so Gitaly can identify them in OptimizeRepository
// and avoid pruning them.
func DerivePoolPath(repositoryID int64) string {
	return deriveDiskPath(praefectPoolPathPrefix, repositoryID)
}
```
