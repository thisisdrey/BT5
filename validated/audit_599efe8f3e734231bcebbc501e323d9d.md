### Title
`FetchIntoObjectPool` fetches objects from any repository into any object pool without verifying pool membership — ([File: internal/gitaly/service/objectpool/fetch_into_object_pool.go])

### Summary
`FetchIntoObjectPool` is analogous to the UStb bug class: the RPC validates one side of a two-repository operation (the *target*/`object_pool`, via `objectpool.FromProto`) but never verifies that the *source* (`origin`) is actually an authorized/legitimate member of that specific pool before pulling its objects in — mirroring the missing `from`-side authorization check in the original finding.

### Finding Description
`validateFetchIntoObjectPoolRequest` only checks that `origin` and `object_pool` are non-empty and share the same storage name: [1](#0-0) 

No check exists that `origin` is actually linked to `object_pool` (i.e., has `objects/info/alternates` pointing at it, or is the pool's designated upstream). `FetchIntoObjectPool` then unconditionally clones/fetches all of `origin`'s refs and objects into the pool: [2](#0-1) [3](#0-2) 

Gitaly's own documentation confirms the intended invariant is purely a convention, not an enforced check: "This task is typically **only executed with the original object pool member** from which the pool has been seeded" — "typically" indicates it's not verified in code: [4](#0-3) 

This is the structural analog of the reported bug: the `WHITELIST_ENABLED` burn path checked `to == address(0)` and `!blacklisted(msg.sender)` but never checked that `from` itself was authorized. Here, Gitaly checks that `object_pool` (the "to" side, the target of the mutation) is a valid pool via `objectpool.FromProto`, and that storage names match, but never checks that `origin` (the "from" side, the source whose objects get pulled) is actually a member of, or otherwise authorized to feed, that pool. Whoever can invoke this MUTATOR RPC (e.g. an attacker with access to the Gitaly RPC surface used during fork/pool-maintenance workflows, or via a crafted `origin` field in the request) can point `origin` at an arbitrary repository on the same storage and have its full object graph merged into a pool of their choosing.

### Impact Explanation
Once foreign objects are pulled into a pool the attacker controls (or is a member of), those objects become reachable through that pool via alternates for every repository linked to it. This allows disclosure/duplication of another repository's Git objects (commits, blobs, trees) outside of the normal repository access-control boundary — a cross-repository object access violation, one of the explicitly in-scope escape classes. Even absent full read access to the pool contents, this also lets an attacker pollute or grow an unrelated pool's data, and could poison future `git fsck`/housekeeping/dedup behavior of the victim's pool.

### Likelihood Explanation
`FetchIntoObjectPool` is a standard MUTATOR RPC in `ObjectPoolService`, invoked as part of the ordinary fork-housekeeping lifecycle (`CreateObjectPool` → `LinkRepositoryToObjectPool` → `FetchIntoObjectPool`). Any caller able to reach this RPC with a crafted `origin` repository field — the only constraint being it resides on the same storage — can trigger the behavior; Gitaly's own RPC layer performs no origin/pool-relationship check, delegating that trust entirely to the caller.

### Recommendation
Before fetching, verify that `origin` is an actual, authorized member of the target `object_pool` (e.g., check that `origin`'s `objects/info/alternates` already resolves to this pool, or cross-check against Rails' `ObjectPoolMembers`/upstream metadata as already used elsewhere in the codebase, e.g. `internal/gitaly/service/internalgitaly/list_pool_upstreams.go`) and reject the request otherwise.

### Proof of Concept
1. Attacker has access to invoke `ObjectPoolService.FetchIntoObjectPool` (e.g., through the same trust boundary that legitimately triggers this RPC during fork maintenance).
2. Attacker crafts `FetchIntoObjectPoolRequest{ Origin: <victim_repo_on_same_storage>, ObjectPool: <attacker_controlled_or_accessible_pool> }`.
3. `validateFetchIntoObjectPoolRequest` passes because both repos share a storage name; no membership check is performed.
4. `objectPool.FetchFromOrigin` executes `git fetch <victim_repo_path> +refs/*:refs/remotes/origin/*` into the pool, pulling all of the victim repository's objects into the attacker-reachable pool. [5](#0-4)

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

**File:** doc/object_pools.md (L47-51)
```markdown
Housekeeping for object pools is handled differently from normal repositories as
it not only involves repacking the pool, but also updating it. The housekeeping
task is thus hosted by the `FetchIntoObjectPool` RPC. This task is typically
only executed with the original object pool member from which the pool has been
seeded and updates the pool by fetching from that member.
```
