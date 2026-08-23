### Title
`LinkRepositoryToObjectPool` links an arbitrary repository to any existing object pool without confirming fork-network membership - (File: internal/gitaly/service/objectpool/link.go)

### Summary
`LinkRepositoryToObjectPool` accepts a caller-supplied `ObjectPool` and `Repository` and links them by writing the pool's path into the repository's `objects/info/alternates` file. The handler validates that the pool path is *structurally* a valid pool repository, but at no point does Gitaly confirm that the pool is actually the pool belonging to the target repository's fork network (i.e., that it was created via `CreateObjectPool` from that repository's own origin, or that the repository is a legitimate member of that pool). This mirrors the reported Comet bug class: an address/identifier supplied by the caller is trusted to be the "official" resource without on-chain (here, on-disk/relationship) verification, and is then used to wire up a critical, privileged path (Git alternates) that other logic implicitly assumes is trustworthy.

### Finding Description
The RPC handler is: [1](#0-0) 

It calls `s.locator.ValidateRepository` on the target repository and `s.poolForRequest` (which resolves through `objectpool.FromProto`) on the pool, then calls `pool.Link(ctx, repo)`. `FromProto` only checks that the relative path matches the pool-directory naming convention and that the repository is a syntactically valid Git repo: [2](#0-1) 

Nowhere in this chain is there a check that the `ObjectPool` being linked is the pool that was actually created (`CreateObjectPool`) for the `Repository`'s own fork network/origin. The low-level `Link` function itself only checks whether the repository is *already* linked to *some* pool, and otherwise happily writes any pool's relative object path into the repository's alternates file: [3](#0-2) 

Once the alternates file points at another pool, all Git plumbing (e.g. `git cat-file`, `git log`, `git fetch`, `git archive`) that operates on the linked repository will transparently resolve and expose *any* object contained in that pool's object database — even objects that are not reachable from the linked repository's own refs — because Git's alternates mechanism grants full read access to every object in the alternate object store, not just ones associated with a particular fork lineage. This is precisely the "object-pool / alternates isolation" bug class called out in the task rules: trusting a caller-supplied identifier (the pool's `Repository{StorageName, RelativePath}`) as if it were verified to belong to the correct trust domain.

The `.proto` definition confirms both fields are plain, independently supplied identifiers with no cross-validation: [4](#0-3) 

### Impact Explanation
If a caller (or an upstream service acting on behalf of an ordinary user creating/importing/forking a project) can invoke `LinkRepositoryToObjectPool` with a pool relative path it does not otherwise "own" — for example by guessing/enumerating another project's Rails-style pool path (`@pools/<hash-prefix>/<hash-prefix>/<sha256>.git`) or by reusing/mis-supplying a `RelativePath` during fork/import flows — it can link its own (attacker-controlled) repository to a foreign object pool. Once linked, the attacker's repository can read out every object present in the victim pool (including private commits/blobs from other forks sharing that pool) via ordinary read RPCs (`GetBlob`, `TreeEntry`, `CatFile`, `Fetch`), constituting cross-repository object disclosure — a serious confidentiality break across supposedly isolated repositories/forks on the same Gitaly storage.

### Likelihood Explanation
Exploitability depends on whether the calling layer (e.g., GitLab Rails, which is the primary intended caller) enforces that the `ObjectPool` argument always corresponds to the repository's actual fork-network pool before invoking this RPC. Gitaly itself provides no defense-in-depth check, so any bug, race condition, or missing authorization check at the calling layer (which does construct these gRPC requests based on user-influenced fork/import operations) directly translates into cross-repository disclosure with no secondary check inside Gitaly. Because pool relative paths are derived deterministically from a SHA-256 of project identifiers (per `IsRailsPoolRepository`/`DerivePoolPath`), an attacker who knows or can enumerate a target's pool path only needs one crafted RPC call.

### Recommendation
Before linking, `LinkRepositoryToObjectPool` (and/or `objectpool.Link`) should verify that the object pool is actually associated with the target repository's origin/fork network — e.g., by recording the originating repository/fork-network ID when the pool is created via `CreateObjectPool`, storing that association (as is already partially tracked via `migrationStateManager.RecordKeyCreation`/`relational.PoolStore`), and requiring `LinkRepositoryToObjectPool` to check that the target repository belongs to the same fork network as the pool's recorded origin before writing the alternates file.

### Proof of Concept
1. As tenant/user A, fork a project, causing Gitaly to create pool `P_A` via `CreateObjectPool` from A's project and link A's repo (`R_A`) to `P_A` via `LinkRepositoryToObjectPool`.
2. As tenant/user B, create an unrelated repository `R_B` (or supply an existing one) that is *not* part of A's fork network.
3. Have the layer that invokes Gitaly (e.g. Rails, if it fails to validate the pool/repo relationship, or a direct authenticated Gitaly client) call `LinkRepositoryToObjectPool(ObjectPool: P_A, Repository: R_B)`.
4. Gitaly's `link.go` handler only checks that `R_B` is a valid repository and `P_A` is a structurally valid pool (`objectpool.FromProto`); it performs the link, writing `P_A`'s object path into `R_B`'s `objects/info/alternates`.
5. From `R_B`, perform `GetBlob`/`CatFile` for object SHAs known to exist only in `P_A`'s object database (i.e., objects belonging to A's private fork network); the objects resolve successfully, demonstrating cross-repository object disclosure with no association check enforced by Gitaly.

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
