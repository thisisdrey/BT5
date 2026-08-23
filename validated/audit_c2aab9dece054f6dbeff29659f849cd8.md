### Title
Cross-repository object disclosure via unauthenticated `origin` repository in `FetchIntoObjectPool` — (File: `internal/gitaly/service/objectpool/fetch_into_object_pool.go`)

### Summary
`FetchIntoObjectPoolRequest` carries two repository identifiers: `object_pool` (marked `target_repository`, the field that Praefect/access-control machinery treats as authoritative) and `origin` (marked `additional_repository`). The RPC handler only checks that the two repositories share a storage name and then performs an unrestricted `git fetch +refs/*` from whatever repository `origin` resolves to into the pool — there is no verification that `origin` is an actual, previously-linked member of that specific object pool.

### Finding Description
`validateFetchIntoObjectPoolRequest` performs exactly one check on the relationship between the two repository fields: [1](#0-0) 

It never checks that `origin` is linked to (an alternate of / a genuine pool member of) `req.GetObjectPool()`. The handler then builds a `localrepo.Repo` directly from the attacker/caller-supplied `origin` field and fetches from it: [2](#0-1) 

The actual fetch pulls in **all refs** (`+refs/*:refs/remotes/origin/*`) from `origin`'s path into the pool with no filtering: [3](#0-2) 

This is structurally the same class of bug as the `bps()` report: the field that gets "validated"/routed for authorization purposes (`object_pool`, `target_repository`) is different from the field that is actually used to perform the sensitive action (`origin`, merely `additional_repository`), and Gitaly performs no cross-check that the two are legitimately related (e.g., that `origin` is actually a pool member per the pool's own alternates bookkeeping, as is enforced elsewhere in `internal/git/objectpool/link.go`'s `linkedToRepository`). Praefect's routing layer also treats `origin` as a mere "additional repository" and only enforces that it lives on the same storage/virtual-storage as the target — it performs no ownership/membership check either: [4](#0-3) 

Once objects are fetched into the pool, they become readable by **every member of that pool** through the shared `objects/info/alternates` mechanism (any pool member can read the pool's objects via `UploadPack`/`CatFile`/etc.), so this effectively becomes a mechanism to smuggle objects from one repository into another repository's object graph that other, less-privileged users can subsequently read.

### Impact Explanation
An actor who can trigger `FetchIntoObjectPool` (directly, or indirectly if any caller path allows influencing the `origin` field independently from the pool identity it's authorized against) can cause private/other-tenant objects to be copied into an object pool. Because pool objects are shared with all pool members via alternates, this can leak commit/blob contents across repository/project boundaries — a cross-repository object disclosure, matching the report's "confused deputy" bug class (validate on A, act on B, no cross-check A↔B).

### Likelihood Explanation
Exploitability depends on whether GitLab Rails' authorization for this internal RPC re-derives and enforces the `origin`↔pool relationship independently for every call, or trusts the caller-supplied pair once a single high-level permission check (e.g., "can manage this pool") passes. Gitaly itself, at the layer analyzed, performs no defense-in-depth check tying `origin` to the specific `object_pool`, so if any upstream authorization gap exists (misconfiguration, a future caller, or a race during project transfer/fork changes), Gitaly will silently fetch and persist cross-tenant objects without complaint.

### Recommendation
In `validateFetchIntoObjectPoolRequest` (or before invoking `FetchFromOrigin`), verify that `origin` is an actual member of `object_pool` — e.g., by checking the origin repository's `objects/info/alternates` resolves to the target pool (mirroring the check already implemented in `linkedToRepository` in `internal/git/objectpool/link.go`), and reject the RPC with `InvalidArgument` otherwise. This closes the gap where the field used for git-level action execution can diverge from the field used for authorization/target resolution.

### Proof of Concept
1. Attacker (or a compromised low-privilege caller with access to invoke `ObjectPoolService.FetchIntoObjectPool` for a pool they administer) creates/owns `object_pool` P and is a legitimate member of P via repo `A`.
2. Attacker issues `FetchIntoObjectPoolRequest{ ObjectPool: P, Origin: <victim repository B on the same storage> }`.
3. `validateFetchIntoObjectPoolRequest` only checks `B.StorageName == P.StorageName` — passes.
4. `objectPool.FetchFromOrigin` executes `git fetch +refs/*:refs/remotes/origin/* <path-to-B>` directly into pool `P`'s object store.
5. All refs/objects from `B` now exist in P's object database and are reachable (e.g., via loose refs under `refs/remotes/origin/*` and their reachable objects) by any repository alternated to P, including repos the attacker fully controls, allowing them to read `B`'s objects without ever having had permission on `B`.

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

**File:** internal/git/objectpool/fetch.go (L74-106)
```go
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

**File:** internal/gitaly/storage/storagemgr/middleware.go (L330-360)
```go
	}

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
```
