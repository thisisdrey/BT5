Confirmed: `validateFetchIntoObjectPoolRequest` in `internal/gitaly/service/objectpool/fetch_into_object_pool.go` only checks that `Origin` and `ObjectPool` are non-nil and share the same `StorageName` — it never checks that `Origin` is actually a registered member of the target pool.

### Title
FetchIntoObjectPool accepts an arbitrary, unrelated `Origin` repository, allowing cross-repository object injection into a shared object pool - (File: internal/gitaly/service/objectpool/fetch_into_object_pool.go)

### Summary
The `FetchIntoObjectPool` RPC is designed to be called only with the object pool's designated upstream/seed member so that housekeeping stays in sync with the pool's authoritative content. However, the RPC handler and its request validator perform no check that the caller-supplied `Origin` repository is actually a member of the target `ObjectPool`. Any caller able to invoke this MUTATOR RPC (e.g. via automated repository/fork actions that route through GitLab's internal API) can supply any repository on the same storage as `Origin`, causing the pool to fetch and permanently retain objects, references and dangling-object protections from a completely unrelated repository.

### Finding Description
`validateFetchIntoObjectPoolRequest` at [1](#0-0)  only verifies that `Origin` and `ObjectPool` are non-nil and share the same storage name. There is no lookup against the pool's membership set (the `pool_members` relation defined in `internal/gitaly/storage/relational/pool_store.go`, exposed via `ListPoolMembers`/`GetPoolForMember`) to confirm `Origin` actually belongs to `ObjectPool`.

The handler then directly builds the origin repo from the request and fetches from it: [2](#0-1) . `FetchFromOrigin` performs a full, non-pruning fetch of `+refs/*:refs/remotes/origin/*` from that origin into the pool and then rescues any resulting dangling objects so they are never deleted: [3](#0-2) . The documentation itself states this task is "typically only executed with the original object pool member," i.e. membership is an assumed invariant, not an enforced one: [4](#0-3) .

This mirrors the reported bug class: two components (the pool's intended, sanctioned data source vs. the RPC that performs the actual mutating fetch) are meant to stay "in sync" only by convention/incentive, but nothing authenticates that the caller-supplied resource passed into the security-relevant operation is the legitimate one. Just as `BalancerLBPSwapper.swap()` will happily use whatever `minAmount` is deposited by any caller instead of the funds `PCVEquityMinter` intended, `FetchIntoObjectPool` will happily fetch from whatever `Origin` repository is supplied instead of the pool's actual member.

### Impact Explanation
Because the pool has no way to selectively delete objects once ingested (dangling objects from any fetch are deliberately kept alive forever, as explained in [5](#0-4) ), a single call with a mismatched `Origin` permanently pollutes the shared object pool with foreign content, references and possibly private objects. Since object pools back forks, every repository linked to the pool via `objects/info/alternates` can subsequently read those objects, resulting in cross-repository object disclosure/leakage and irreversible storage growth (repeated housekeeping and repacking of unrelated objects) for the pool. Repeated calls could also desynchronize the pool's expected content from what its true members expect, similar to how the report's exploit desynchronizes `PCVEquityMinter`/`BalancerLBPSwapper` state.

### Likelihood Explanation
`FetchIntoObjectPool` is a plain MUTATOR RPC with no per-call authorization tying `Origin` to `ObjectPool` membership; the only gate is the general gRPC auth token/transaction plumbing that all Gitaly RPCs share, not an object-pool-specific check. Any code path that can construct this RPC (directly, through GitLab's internal API automation, or a compromised/forked automation flow) with attacker-influenced `Origin`/`ObjectPool` fields on the same storage can trigger this. The `Praefect` coordinator test even demonstrates that `Origin` and `ObjectPool` can be arbitrary "target and additional repository" combinations that are simply routed through with no membership validation: [6](#0-5) .

### Recommendation
Before performing the fetch, validate that `req.GetOrigin()`'s relative path is registered as a member of `req.GetObjectPool()` (e.g. via the `PoolStore.ListPoolMembers`/`GetPoolForMember` APIs already available in `internal/gitaly/storage/relational/pool_store.go`), and reject the RPC with `InvalidArgument`/`PermissionDenied` if it is not. This closes the gap analogous to the recommendation in the source report ("authenticate the call ... to prevent this issue").

### Proof of Concept
1. Create object pool `P` seeded from repository `A` (`CreateObjectPool` + `LinkRepositoryToObjectPool`), such that `A` is the only legitimate member of `P`.
2. Create an unrelated repository `B` on the same storage, containing objects/refs that should never be exposed to `P`'s members.
3. Call `FetchIntoObjectPool` with `ObjectPool = P`, `Origin = B` (as shown feasible in [7](#0-6) , which only requires `Origin`/`ObjectPool` to share storage).
4. Observe that `P` now contains `B`'s refs under `refs/remotes/origin/*` and retains `B`'s objects indefinitely via dangling-ref protection, all without any check that `B` is a member of `P`.

### Citations

**File:** internal/gitaly/service/objectpool/fetch_into_object_pool.go (L26-32)
```go
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

**File:** internal/git/objectpool/fetch.go (L59-110)
```go
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

	if err := o.rescueDanglingObjects(ctx); err != nil {
		return fmt.Errorf("rescuing dangling objects: %w", err)
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

**File:** doc/object_pools.md (L78-97)
```markdown
## Dangling Objects

When fetching from pool members into the object pool, then any force-updated
references may cause objects in the pool to not be referenced anymore. For
normal repositories, it is perfectly fine to delete those references after a
certain time. In the context of object pools, any other member of the pool may
still use any of those unreferenced objects. Deleting them would thus
potentially cause corrupt repositories.

This issue is kind of unsolvable: there is no point in time where it's safe to
delete objects from the object pool, as we do not know which repositories may be
linked to it. And even if we knew, we cannot determine all references of all
repositories at once in a race-free manner. We thus must consider each object to
still be referenced somewhere.

As a safeguard to not lose any objects by accident, we thus create dangling
references in the object pool after the fetch in `FetchIntoObjectPool`. For each
dangling object, a reference `refs/dangling/$OID` is created which points into
the object. This assures that each object is still referenced.

```

**File:** internal/praefect/coordinator_test.go (L236-269)
```go
			desc: "target and additional repository",
			setup: func(t *testing.T, rs datastore.RepositoryStore) setupData {
				targetRepo := &gitalypb.Repository{
					StorageName:  "praefect",
					RelativePath: gittest.NewRepositoryName(t),
				}
				additionalRepo := &gitalypb.Repository{
					StorageName:  "praefect",
					RelativePath: gittest.NewRepositoryName(t),
				}

				targetRepoID := createRepo(t, rs, "praefect", targetRepo.GetRelativePath(), "rewritten-target")
				createRepo(t, rs, "praefect", additionalRepo.GetRelativePath(), "rewritten-additional")

				return setupData{
					method: "/gitaly.ObjectPoolService/FetchIntoObjectPool",
					request: &gitalypb.FetchIntoObjectPoolRequest{
						Origin: additionalRepo,
						ObjectPool: &gitalypb.ObjectPool{
							Repository: targetRepo,
						},
					},
					expectedRewrittenRequest: &gitalypb.FetchIntoObjectPoolRequest{
						Origin: &gitalypb.Repository{
							StorageName:  "praefect-internal-1",
							RelativePath: "rewritten-additional",
						},
						ObjectPool: &gitalypb.ObjectPool{
							Repository: &gitalypb.Repository{
								StorageName:  "praefect-internal-1",
								RelativePath: "rewritten-target",
							},
						},
					},
```

**File:** internal/gitaly/service/objectpool/fetch_into_object_pool_test.go (L141-145)
```go
		_, err = client.FetchIntoObjectPool(ctx, &gitalypb.FetchIntoObjectPoolRequest{
			ObjectPool: poolProto,
			Origin:     repo,
		})
		require.NoError(t, err)
```
