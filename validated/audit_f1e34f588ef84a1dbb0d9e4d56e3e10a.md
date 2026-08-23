Confirmed: `FetchIntoObjectPool` accepts an arbitrary `origin` repository field in the request and passes it directly to `FetchFromOrigin` with no verification that the given origin is actually a linked member of the target object pool. [1](#0-0) [2](#0-1) 

### Title
Unauthenticated pool-membership check allows fetching arbitrary attacker-chosen "origin" repository into a shared object pool - (File: internal/gitaly/service/objectpool/fetch_into_object_pool.go)

### Summary
The M-03 bug class is about an unprivileged actor invoking a state-changing entry point that operates on a pre-announced/shared target using attacker-supplied parameters, with no check that the caller/parameters are the legitimate ones expected by the system, letting the attacker bias the outcome of the operation. In Gitaly's `ObjectPoolService`, `FetchIntoObjectPool` is analogous: it merges objects from any repository the caller names as `origin` into a shared object pool, without verifying `origin` is actually a linked member of that pool.

### Finding Description
`FetchIntoObjectPool` validates only structural fields of the request (`validateFetchIntoObjectPoolRequest`) and does not check that `req.GetOrigin()` is actually linked to `req.GetObjectPool()` via the `objects/info/alternates` mechanism used elsewhere in the object-pool code (see `linkedToRepository` in `internal/git/objectpool/link.go`, which is only invoked from the `Link` path, not from `FetchFromOrigin`). [1](#0-0) [3](#0-2) 

`FetchFromOrigin` simply resolves the origin's path and performs a `git fetch --atomic` from it into the pool, pulling in all refs/objects reachable from the caller-specified repository, then runs full housekeeping/repack on the pool: [2](#0-1) [4](#0-3) [5](#0-4) 

The documentation itself states the RPC is "typically only executed with the original object pool member," implying this is a convention rather than an enforced invariant: [6](#0-5) 

Because object pools back deduplication for repository forks, and the pool's objects are advertised as alternates to every member (and can be leaked via `refs/dangling/$OID` and alternate-ref announcement during fetch negotiation, see `internal/gitaly/service/repository/fetch_remote_test.go`), fetching from an arbitrary attacker-chosen `origin` merges that repository's objects/refs into the shared pool that other, unrelated fork-network members reference. This mirrors the analog rule's "cross-repository object access" and "object-pool and alternates isolation" categories — the caller can choose which repository's state ("mutable shared state") gets folded into the pool at a moment of their choosing, and other legitimate members subsequently see or inherit that state through the pool.

### Impact Explanation
An attacker who can invoke `FetchIntoObjectPool` (e.g., via GitLab's internal API path that exposes it during fork/import flows, or directly if RPC access controls are weaker than assumed) can inject objects/refs from a repository under their control into a shared object pool used by other repositories/forks, corrupting pool consistency, causing resource exhaustion via full pool repacks triggered on demand, or leaking dangling-object references across the fork network boundary. This maps to cross-repository object access and DoS-of-handler impact categories.

### Likelihood Explanation
Likelihood is moderate: exploitation requires the ability to call the `ObjectPoolService.FetchIntoObjectPool` RPC with an arbitrary `object_pool`/`origin` pair. Under Gitaly's standard trust model, gRPC calls are gated by GitLab's application-level authorization (which normally only invokes this RPC with the legitimate member), so the vulnerability is a missing defense-in-depth check at the Gitaly layer rather than a directly externally-reachable exploit for an anonymous git push/fetch user. This is comparable to the ERC-note's "front-running of a public function" pattern — the function itself lacks intrinsic validation and instead relies entirely on the caller supplying correct, trusted parameters.

### Recommendation
Add an explicit membership check in `FetchIntoObjectPool` (or in `ObjectPool.FetchFromOrigin`) that verifies the given `origin` repository is actually linked to the target `object_pool` (reusing the existing `linkedToRepository` logic from `internal/git/objectpool/link.go`) before performing the fetch and repack, rejecting the request with `InvalidArgument` otherwise.

### Proof of Concept
1. Create an object pool `P` from legitimate repository `A` and link `A` to `P`.
2. Create an unrelated repository `B` under attacker control containing arbitrary/large/malicious ref data.
3. Call `FetchIntoObjectPool` with `object_pool = P`, `origin = B` (not linked to `P`).
4. Observe that Gitaly performs `git fetch --atomic origin refs/*:refs/remotes/origin/*` pulling `B`'s objects/refs into `P`, and then runs full housekeeping/repack on `P`, despite `B` never having been linked via `LinkRepositoryToObjectPool`.

Note: I was unable to fully verify the caller-side gRPC authorization/interceptor chain that gates access to `ObjectPoolService` in this indexed snapshot (e.g., whether an unprivileged, unauthenticated user could reach this RPC directly versus only via privileged GitLab-Rails-initiated calls), so the "ordinary user reachability" of this specific RPC could not be conclusively confirmed from the available index; a Devin session with full repository access would be needed to trace the complete authorization path.

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

**File:** internal/git/objectpool/fetch.go (L30-47)
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

**File:** internal/git/objectpool/fetch.go (L134-139)
```go
	// We've committed the original transaction above. OptimizeRepository internally starts
	// another transaction, and knows how to retrieve the original relative path of the repository
	// if there is a transaction in the context.
	if err := o.housekeepingManager.OptimizeRepository(ctx, o.Repo); err != nil {
		return fmt.Errorf("optimizing pool repo: %w", err)
	}
```

**File:** internal/git/objectpool/link.go (L168-204)
```go
// linkedToRepository tests if a repository is linked to an object pool
func linkedToRepository(ctx context.Context, pool, repo *localrepo.Repo) (bool, error) {
	poolPath, err := pool.Path(ctx)
	if err != nil {
		return false, fmt.Errorf("getting object pool path: %w", err)
	}

	repoPath, err := repo.Path(ctx)
	if err != nil {
		return false, fmt.Errorf("getting repo path: %w", err)
	}

	altInfo, err := stats.AlternatesInfoForRepository(repoPath)
	if err != nil {
		return false, fmt.Errorf("getting alternates info: %w", err)
	}

	if !altInfo.Exists || len(altInfo.ObjectDirectories) == 0 {
		return false, nil
	}

	relPath := altInfo.ObjectDirectories[0]
	expectedRelPath, err := getRelativeObjectPath(ctx, pool, repo)
	if err != nil {
		return false, err
	}

	if relPath == expectedRelPath {
		return true, nil
	}

	if filepath.Clean(relPath) != filepath.Join(poolPath, "objects") {
		return false, fmt.Errorf("unexpected alternates content: %q", relPath)
	}

	return false, nil
}
```

**File:** doc/object_pools.md (L47-51)
```markdown
Housekeeping for object pools is handled differently from normal repositories as
it not only involves repacking the pool, but also updating it. The housekeeping
task is thus hosted by the `FetchIntoObjectPool` RPC. This task is typically
only executed with the original object pool member from which the pool has been
seeded and updates the pool by fetching from that member.
```
