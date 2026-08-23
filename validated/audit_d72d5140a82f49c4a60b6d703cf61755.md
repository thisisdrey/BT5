## Title
`FetchIntoObjectPool` accepts an arbitrary, unrelated `origin` repository and only checks storage equality, allowing cross-repository object pool contamination - (File: internal/gitaly/service/objectpool/fetch_into_object_pool.go)

### Summary
The external report describes smart-contract modules that trust caller-supplied "dispute"/"request" data without validating it is actually associated with the `requestId` recorded on-chain, letting an attacker substitute an unrelated object (e.g. a self-controlled ERC20 token) into a privileged code path. The equivalent pattern in Gitaly is `FetchIntoObjectPoolRequest`: the RPC takes two independently supplied repository references — `origin` and `object_pool` — and the only relationship check performed between them is that they share the same storage name. There is no verification that `origin` is actually a member of, or otherwise legitimately associated with, the target object pool.

### Finding Description
`FetchIntoObjectPool` is defined to "fetch all references from a pool member into an object pool so that objects shared between this repository and other pool members can be deduplicated" [1](#0-0) . The handler builds the `objectPool` from `req.GetObjectPool()` and the `origin` repository straight from `req.GetOrigin()`, with essentially no cross-check that `origin` is a real member of that pool: [2](#0-1) 

The only validation performed is `validateFetchIntoObjectPoolRequest`, which checks that `origin` and `object_pool` are non-nil and share the same storage name — nothing more: [3](#0-2) 

The actual git-level fetch simply performs `git fetch <originPath> +refs/*:refs/remotes/origin/*` against the pool, pulling in every ref (and reachable object) from whatever repository was supplied as `origin`: [4](#0-3) 

Because object pool members read objects out of the pool via Git alternates (`objects/info/alternates`) [5](#0-4) , any object pulled into the pool from an attacker-chosen `origin` becomes reachable to every other repository already linked to that pool through the alternates mechanism. Unlike the mitigated analog in `GetObjectDirectorySize`/quarantine handling — where the object directory or quarantine directory is validated against a repository-specific prefix derived from the repository's own relative path (`storage.QuarantineDirectoryPrefix`) before being trusted [6](#0-5) [7](#0-6)  — `FetchIntoObjectPool` performs no such binding check between the supplied `origin` and the `object_pool`'s recorded membership.

Partitioning-layer logic does place a pool and "about to be connected" repositories in the same partition when an additional repository is present in the request [8](#0-7) , but this only constrains storage/partition co-location for transactional consistency — it is not an authorization or membership check confirming `origin` legitimately belongs to the object pool being fetched into.

### Impact Explanation
An attacker who can invoke `FetchIntoObjectPool` (or trigger it indirectly through GitLab Rails' fork/pool housekeeping flow) with a crafted `origin` field pointing at a repository other than the pool's intended primary member can inject that repository's objects into the shared object pool. Since deduplication via alternates makes pool objects reachable from every linked member, this can lead to cross-repository object disclosure/contamination — objects from one repository becoming visible to other, unrelated repositories that share the pool, undermining repository object isolation guarantees that the rest of the codebase (quarantine directory prefix checks, alternates chain restrictions, etc.) is specifically designed to protect.

### Likelihood Explanation
Exploitability depends on the caller being able to supply an arbitrary `origin` and `object_pool` pair to this RPC (or on GitLab Rails' orchestration of fork/pool housekeeping not further constraining the pairing before calling Gitaly), analogous to the report's requirement that an ordinary user can supply crafted request/dispute data to a module function. Given the RPC-level code performs no membership validation whatsoever beyond storage-name equality, the likelihood is contingent primarily on how strictly the calling layer (Rails) constrains which `origin`/`object_pool` pairs it will ever request — a boundary that lives outside Gitaly itself.

### Recommendation
- **Short term:** In `validateFetchIntoObjectPoolRequest`/`FetchIntoObjectPool`, verify that `origin` is actually linked to (or equal to the primary seed of) `object_pool` — e.g. by checking the origin repository's `objects/info/alternates` resolves to the given pool, mirroring the existing `QuarantineDirectoryPrefix`-style binding check used elsewhere — before performing the fetch.
- **Long term:** Add regression tests that attempt to fetch from an arbitrary, unrelated `origin` into an existing object pool and assert the RPC rejects the combination, extending the adversarial input coverage already present for `GetObjectDirectorySize`'s quarantine-swap test case.

### Proof of Concept
1. Create two independent repositories, `RepoA` (an object pool member with pool `PoolA`) and `RepoB` (unrelated, containing sensitive objects).
2. Call `FetchIntoObjectPool` with `Origin: RepoB` and `ObjectPool: PoolA` (same storage name as `RepoB`).
3. `validateFetchIntoObjectPoolRequest` passes because only storage-name equality is checked [9](#0-8) .
4. `FetchFromOrigin` executes `git fetch <RepoB path> +refs/*:refs/remotes/origin/*` into `PoolA`, importing `RepoB`'s objects into the pool [10](#0-9) .
5. Any other repository already linked to `PoolA` via alternates can now read `RepoB`'s objects through the shared pool.

### Citations

**File:** proto/objectpool.proto (L80-88)
```text
  // FetchIntoObjectPool fetches all references from a pool member into an object pool so that
  // objects shared between this repository and other pool members can be deduplicated. This RPC
  // will perform housekeeping tasks after the object pool has been updated to ensure that the pool
  // is in an optimal state.
  rpc FetchIntoObjectPool(FetchIntoObjectPoolRequest) returns (FetchIntoObjectPoolResponse) {
    option (op_type) = {
      op: MUTATOR
    };
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

**File:** internal/git/objectpool/fetch.go (L30-106)
```go
// FetchFromOrigin initializes the pool and fetches the objects from its origin repository
func (o *ObjectPool) FetchFromOrigin(ctx context.Context, origin *localrepo.Repo, newLocalRepo LocalRepoFactory) error {
	if !o.Exists(ctx) {
		return structerr.NewInvalidArgument("object pool does not exist")
	}

	originPath, err := origin.Path(ctx)
	if err != nil {
		return fmt.Errorf("computing origin repo's path: %w", err)
	}

	if err := o.housekeepingManager.CleanStaleData(ctx, o.Repo, housekeeping.DefaultStaleDataCleanup()); err != nil {
		return fmt.Errorf("cleaning stale data: %w", err)
	}

	if err := o.logStats(ctx, "before fetch"); err != nil {
		return fmt.Errorf("computing stats before fetch: %w", err)
	}

	// Ideally we wouldn't want to prune old references at all so that we can keep alive all
	// objects without having to create loads of dangling references. But unfortunately keeping
	// around old refs can lead to D/F conflicts between old references that have since
	// been deleted in the pool and new references that have been added in the pool member we're
	// fetching from. E.g. if we have the old reference `refs/heads/branch` and the pool member
	// has replaced that since with a new reference `refs/heads/branch/conflict` then
	// the fetch would now always fail because of that conflict.
	//
	// Due to the lack of an alternative to resolve that conflict we are thus forced to enable
	// pruning. This isn't too bad given that we know to keep alive the old objects via dangling
	// refs anyway, but I'd sleep easier if we didn't have to do this.
	//
	// Note that we need to perform the pruning separately from the fetch: if the fetch is using
	// `--atomic` and `--prune` together then it still wouldn't be able to recover from the D/F
	// conflict. So we first to a preliminary prune that only prunes refs without fetching
	// objects yet to avoid that scenario.
	if err := o.pruneReferences(ctx, origin); err != nil {
		return fmt.Errorf("pruning references: %w", err)
	}

	objectHash, err := o.Repo.ObjectHash(ctx)
	if err != nil {
		return fmt.Errorf("detecting object hash: %w", err)
	}

	var stderr bytes.Buffer
	if err := o.Repo.ExecAndWait(ctx,
		gitcmd.Command{
			Name: "fetch",
			Flags: []gitcmd.Option{
				gitcmd.Flag{Name: "--quiet"},
				gitcmd.Flag{Name: "--atomic"},
				// We already fetch tags via our refspec, so we don't
				// want to fetch them a second time via Git's default
				// tag refspec.
				gitcmd.Flag{Name: "--no-tags"},
				// We don't need FETCH_HEAD, and it can potentially be hundreds of
				// megabytes when doing a mirror-sync of repos with huge numbers of
				// references.
				gitcmd.Flag{Name: "--no-write-fetch-head"},
				// Disable showing forced updates, which may take a considerable
				// amount of time to compute. We don't display any output anyway,
				// which makes this computation kind of moot.
				gitcmd.Flag{Name: "--no-show-forced-updates"},
			},
			Args: []string{originPath, objectPoolRefspec},
		},
		gitcmd.WithRefTxHook(objectHash, o.Repo),
		gitcmd.WithStderr(&stderr),
		gitcmd.WithConfig(gitcmd.ConfigPair{
			// Git is so kind to point out that we asked it to not show forced updates
			// by default, so we need to ask it not to do that.
			Key: "advice.fetchShowForcedUpdates", Value: "false",
		}),
	); err != nil {
		return fmt.Errorf("fetch into object pool: %w, stderr: %q", err,
			stderr.String())
	}
```

**File:** doc/object_pools.md (L9-12)
```markdown

The sharing of objects for a given repository and its object pool is done via
alternate object directories which Gitaly sets up when linking a repository to
an object pool by writing the `objects/info/alternates` file.
```

**File:** internal/gitaly/storage/locator.go (L201-212)
```go
// QuarantineDirectoryPrefix returns a prefix for use in the temporary directory. The prefix is
// based on the relative repository path and will stay stable for any given repository. This allows
// us to verify that a given quarantine object directory indeed belongs to the repository at hand.
// Ideally, this function would directly be located in the quarantine module, but this is not
// possible due to cyclic dependencies.
func QuarantineDirectoryPrefix(repo Repository) string {
	hash := [20]byte{}
	if repo != nil {
		hash = sha1.Sum([]byte(repo.GetRelativePath()))
	}
	return fmt.Sprintf("quarantine-%x-", hash[:8])
}
```

**File:** internal/git/localrepo/paths.go (L53-75)
```go
	if !isTransactionQuarantineDir {
		// We need to check whether the relative object directory as given by the repository is
		// a valid path. This may either be a path in the Git repository itself, where it may either
		// point to the main object directory storage or to an object quarantine directory as
		// created by git-receive-pack(1). Alternatively, if that is not the case, then it may be a
		// manual object quarantine directory located in the storage's temporary directory. These
		// have a repository-specific prefix which we must check in order to determine whether the
		// quarantine directory does in fact belong to the repo at hand.
		if _, origError := storage.ValidateRelativePath(repoPath, objectDirectoryPath); origError != nil {
			tempDir, err := repo.locator.TempDir(repo.GetStorageName())
			if err != nil {
				return "", structerr.NewInvalidArgument("getting storage's temporary directory: %w", err)
			}

			expectedQuarantinePrefix := filepath.Join(tempDir, storage.QuarantineDirectoryPrefix(repo))
			absoluteObjectDirectoryPath := filepath.Join(repoPath, objectDirectoryPath)

			// The relative path is outside of the repository
			if !strings.HasPrefix(absoluteObjectDirectoryPath, expectedQuarantinePrefix) {
				return "", structerr.NewInvalidArgument("not a valid relative path: %w", origError)
			}
		}
	}
```

**File:** internal/gitaly/storage/storagemgr/middleware.go (L332-357)
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
```
