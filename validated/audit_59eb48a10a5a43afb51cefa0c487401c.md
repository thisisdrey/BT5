Based on my analysis, I found a valid analog in Gitaly's `LinkRepositoryToObjectPool` RPC.

### Title
Cross-Repository Object Access via Arbitrary `object_pool` in `LinkRepositoryToObjectPool` - (File: `internal/gitaly/service/objectpool/link.go`)

### Summary
`LinkRepositoryToObjectPool` accepts two independently-controlled repository fields — the `repository` (target, subject to authorization by Rails/Praefect) and the `object_pool` (an "additional repository" that is only validated for being *a* valid pool directory, not for being *the caller's* pool). At the Gitaly layer there is no check binding the two together beyond storage/partition co-location, mirroring the `mintSynth()` flaw where `to`/`from` were independently attacker-influenced instead of being tied to the same authorized principal.

### Finding Description
`LinkRepositoryToObjectPool` validates only that `repository` is a legitimate, existing repository the caller owns, and that `object_pool` resolves to *some* structurally valid pool repository: [1](#0-0) 

`s.poolForRequest` -> `objectpool.FromProto` only checks that the pool path matches the pool naming/location convention and is a valid git repository — it does not verify that the pool is the one associated with the `repository` the caller has permission to modify: [2](#0-1) 

Once "linked," `objectpool.Link` writes the pool's path into the repository's `objects/info/alternates` file, causing Git to treat all pool objects as loose alternates readable during future fetches, clones, and reads of the target repository: [3](#0-2) 

Because pool paths are deterministic (`@pools/<sha256-derived>/...` from a repository/pool ID, not a secret) as seen in `DerivePoolPath`, an attacker who can compute or enumerate another repository's pool path can supply it as `object_pool` while supplying a repository they themselves fully control as `repository`: [4](#0-3) 

The protobuf schema explicitly marks `object_pool` as merely an `additional_repository` (not itself authorization-checked in the same way as `target_repository`), so the object pool identity is essentially caller-supplied data used to redirect object access, analogous to `mintSynth()`'s caller-controlled `to` address redirecting a benefit that should only flow to an authorized party: [5](#0-4) 

The `beginTransactionForRepository` middleware only enforces that target and additional repositories share the same storage/partition — it performs no ownership check that the additional (pool) repository is one the caller is entitled to link against: [6](#0-5) 

### Impact Explanation
If a caller can drive `LinkRepositoryToObjectPool` with an `object_pool` belonging to an unrelated (and possibly private) repository's fork network, all pool objects (commits, blobs, trees) become readable from the attacker's own linked repository — for example via subsequent `Fetch`/`Clone`/`CommitService` reads of the now-linked repo. This is a cross-repository object disclosure, matching the "object-pool and alternates isolation" bug class called out as in-scope. The severity depends on how difficult it is in practice to guess or discover a victim's pool relative path (it's derived from a numeric repository ID via SHA256 truncation, not a high-entropy secret), and on whether GitLab Rails imposes an independent authorization check on the `object_pool` field before proxying the RPC.

### Likelihood Explanation
Exploitability depends entirely on whether the higher-level authorization layer (GitLab Rails, which is out of scope for this repo) verifies that the caller has access to the *specific* object pool named in the request, separate from verifying access to `repository`. Within Gitaly itself, as shown above, there is no such binding check — the RPC handler trusts that anything reaching it with a structurally valid pool path is an authorized link. Given pool paths are derived from small, sequential repository IDs (see `deriveDiskPath`), enumeration is feasible if this RPC is reachable without matching authorization at the Rails layer.

### Recommendation
Tie authorization of `object_pool` to the actual fork/pool relationship recorded for `repository` (e.g., require that the pool was created from `repository`'s own project/fork network, verified via a project/pool ownership record) rather than trusting any structurally valid pool path supplied in the request. At minimum, Gitaly-side code should refuse to link a repository to a pool unless there is proof (e.g., matching fork-network metadata passed through from Rails) that the pool is legitimately associated with the repository or one of its forks, rather than relying solely on `IsPoolRepository`/`FromProto`'s path-format checks.

### Proof of Concept
1. Attacker creates/owns repository `R` on the same Gitaly storage as a victim's object pool `P` (pools are typically created for any forked project via `CreateObjectPool`).
2. Attacker computes/derives `P`'s relative path using the deterministic scheme in `DerivePoolPath`/`IsRailsPoolRepository` (based on the victim project or pool's numeric ID), producing a syntactically valid pool path.
3. Attacker issues `LinkRepositoryToObjectPool{ Repository: R, ObjectPool: P }`. `link.go`'s handler validates only that `R` is a real repo and `P` structurally looks like a pool, per `internal/gitaly/service/objectpool/link.go:10-28` and `internal/git/objectpool/pool.go:46-91`.
4. `objectpool.Link` writes `P`'s path into `R`'s `objects/info/alternates`, per `internal/git/objectpool/link.go:25-84`.
5. Attacker performs a `git clone`/fetch or CommitService read against `R`; Git transparently resolves objects through the alternate, exposing `P`'s (and by extension the victim repository's deduplicated) objects to the attacker.

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

**File:** internal/gitaly/storage/repository_path.go (L40-59)
```go
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
