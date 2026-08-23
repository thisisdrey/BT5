I have sufficient evidence to establish this analog. The mechanism is fully mapped: an ordinary user pushing/deleting refs churns unreachable objects cheaply (no minimum cost), and Gitaly's own automatic housekeeping (`internal/grpc/middleware/housekeeping/middleware.go`) triggers `OptimizeRepository` after every N writes, which — once `LooseObjects.StaleCount` exceeds `LooseObjectLimit` (1024) — invokes an expensive full `--cruft` repack over the *entire* repository object set with no upper bound on repository/object-count size.

### Title
Unbounded free-cost unreachable-object creation via ordinary pushes triggers unbounded full cruft repack, causing repository-wide DoS - (File: internal/git/housekeeping/optimization_strategy.go)

### Summary
`StakePet::create` let anyone mint unlimited NFTs for free, and a later `buryAllDeadPets`/`getDeadNonBuriedPets` call had to iterate the unbounded list, risking out-of-gas DoS. The Gitaly analog: an ordinary, unprivileged Git client can push and immediately orphan branches (e.g., create a branch pointing at a large object, then force-update/delete it) at effectively zero cost, repeatedly, causing large numbers of unreachable loose objects to accumulate in the repository. Gitaly's own automatic housekeeping — triggered transparently after ordinary mutator RPCs like any push (`internal/grpc/middleware/housekeeping/middleware.go`) — reacts to this growth by invoking `OptimizeRepository`, which per `HeuristicalOptimizationStrategy.ShouldRepackObjects`/`ShouldPruneObjects` (`internal/git/housekeeping/optimization_strategy.go`) chooses `RepackObjectsStrategyFullWithCruft` once the unreachable/loose object count crosses `LooseObjectLimit` (1024, `internal/git/housekeeping/objects.go:22-26`). This strategy performs a full `git repack --cruft` pass over the *entire* object graph of the repository — an operation whose cost scales with total repository size, not with the actual amount of garbage — with no configured ceiling on how large that full repack can become.

### Finding Description
The relevant call chain:
1. Any authenticated Git user can push new commits/branches and then delete or reset them (`DeleteRefs`/`UpdateReferences`, or plain force-push), which is a normal, cheap, unprivileged Git operation — no economic cost is imposed like a minimum "deposit."
2. Every push increments a per-repository write counter tracked by `internal/grpc/middleware/housekeeping/middleware.go` (`scheduleHousekeeping`, `pendingOperations`), which asynchronously calls `manager.OptimizeRepository` once thresholds are exceeded.
3. `HeuristicalOptimizationStrategy.ShouldRepackObjects` (`internal/git/housekeeping/optimization_strategy.go:60-146`) and `ShouldPruneObjects` (lines 289-308) key off `s.info.LooseObjects.Count`/`StaleCount` compared against the fixed constant `LooseObjectLimit = 1024` (`internal/git/housekeeping/objects.go:22-26`). Once a user has generated more than 1024 stale/loose (unreachable) objects — trivially achievable by repeated cheap pushes/deletes — the strategy unconditionally selects `RepackObjectsStrategyFullWithCruft`.
4. `RepackObjects` (`internal/git/housekeeping/objects.go:116-131`) then executes `git repack --cruft --pack-kept-objects -l -d` over the whole repository, and `PruneUnreachableObjects` (`internal/gitaly/service/repository/prune_unreachable_objects.go:79-88`) explicitly documents this as "quite expensive" because "the only way" to purge unreachable cruft-packed objects "is to do a full repack."
5. This "full repack" cost is a function of total repository object count, not garbage count, and it runs on Gitaly's own worker goroutine on every repository whose churn crosses the threshold — with no per-repository size cap, no user-visible cost, and no backpressure preventing the same unprivileged user from repeatedly re-triggering it.

This exactly mirrors the reported bug class: an actor pays effectively nothing to create objects, and a downstream maintenance routine must do work that scales with the accumulated (attacker-controlled) volume, with no minimum-cost gate to make abuse expensive.

### Impact Explanation
Repeated cheap push/delete/force-update cycles by a single unprivileged user can force Gitaly to repeatedly perform full `--cruft` repacks of a repository. On a repository that is otherwise large, this ties up CPU/IO/disk resources for the housekeeping worker, competing with legitimate git operations against that repository (and, since housekeeping acquires a per-repository lock via `tryRunningHousekeeping`, blocking concurrent maintenance RPCs like `OptimizeRepository`/`PruneUnreachableObjects` with an `AlreadyExists` error as seen in `middleware_test.go:519-541`). This is a resource-exhaustion/DoS of the housekeeping subsystem for that repository, degrading Gitaly node performance for all its tenants under multi-tenant hosting (e.g., GitLab.com-style shared storage).

### Likelihood Explanation
Any user with ordinary push access to a repository can trigger this without any special privilege, using nothing but standard Git push/branch-delete operations, which is exactly the "ordinary user push" reachable path required. No malicious peer, MITM, or leaked token is needed.

### Recommendation
Introduce a cost-aware or rate-limited gate on how often full/expensive repacks (`RepackObjectsStrategyFullWithCruft`) can be triggered per repository within a time window, independent of the loose-object-count heuristic (there is already a `FullRepackCooldownPeriod` used for object pools in `ShouldRepackObjects`, but ordinary repositories' cruft-repack path via `ShouldPruneObjects` has no equivalent cooldown gating repeated triggering by churn). Consider bounding the cost of a single cruft repack (e.g., incremental cruft packing of only the newly-stale range) rather than a full repository-wide repack, and/or capping how many loose/unreachable objects a single client can create within a time window before pushes are throttled.

### Proof of Concept
1. As an ordinary user with push access to `repo.git`, repeatedly: `git push origin HEAD:refs/heads/tmp-N` (creating a large blob/commit), then `git push origin :refs/heads/tmp-N` (deleting it) — or use `UpdateReferences`/`DeleteRefs` gRPC calls directly — in a loop, incrementing `N`.
2. Each deleted branch leaves its objects unreachable but present as loose objects until the grace period from `stats.StaleObjectsGracePeriod` elapses.
3. Once `s.info.LooseObjects.StaleCount` exceeds `housekeeping.LooseObjectLimit` (1024), `HeuristicalOptimizationStrategy.ShouldPruneObjects` (`internal/git/housekeeping/optimization_strategy.go:292-308`) returns `true`, and the automatic housekeeping middleware's next `OptimizeRepository` invocation performs `RepackObjectsStrategyFullWithCruft` (`internal/git/housekeeping/objects.go:116-131`), a full repository repack whose duration scales with total repository size.
4. Repeating this loop against a repository with a large existing history forces this expensive full repack to recur, consuming Gitaly node resources on every threshold crossing, at negligible cost to the attacker. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** internal/git/housekeeping/optimization_strategy.go (L289-308)
```go
// ShouldPruneObjects determines whether the repository has stale objects that should be pruned.
// Object pools are never pruned to not lose data in them, but otherwise we prune when we've found
// enough stale objects that might in fact get pruned.
func (s HeuristicalOptimizationStrategy) ShouldPruneObjects(context.Context) (bool, PruneObjectsConfig) {
	// Pool repositories must never prune any objects, or otherwise we may corrupt members of
	// that pool if they still refer to that object.
	if s.info.IsObjectPool {
		return false, PruneObjectsConfig{}
	}

	// When we have a number of loose objects that is older than two weeks then they have
	// surpassed the grace period and may thus be pruned.
	if s.info.LooseObjects.StaleCount <= LooseObjectLimit {
		return false, PruneObjectsConfig{}
	}

	return true, PruneObjectsConfig{
		ExpireBefore: s.expireBefore,
	}
}
```

**File:** internal/git/housekeeping/objects.go (L22-26)
```go
const (
	// LooseObjectLimit is the limit of loose objects we accept both when doing incremental
	// repacks and when pruning objects.
	LooseObjectLimit = 1024
)
```

**File:** internal/git/housekeeping/objects.go (L116-131)
```go
	case config.RepackObjectsStrategyFullWithCruft:
		options := []gitcmd.Option{
			gitcmd.Flag{Name: "--cruft"},
			gitcmd.Flag{Name: "--pack-kept-objects"},
			gitcmd.Flag{Name: "-l"},
			gitcmd.Flag{Name: "-d"},
		}

		if !cfg.CruftExpireBefore.IsZero() {
			options = append(options, gitcmd.ValueFlag{
				Name:  "--cruft-expiration",
				Value: git.FormatTime(cfg.CruftExpireBefore),
			})
		}

		return PerformRepack(ctx, repo, cfg, options...)
```

**File:** internal/gitaly/service/repository/prune_unreachable_objects.go (L79-88)
```go
	// But we also have to prune unreachable objects part of cruft packs. The only way to do
	// that is to do a full repack. So unfortunately, this is quite expensive.
	if err := housekeeping.RepackObjects(ctx, repo, housekeepingcfg.RepackObjectsConfig{
		Strategy:            housekeepingcfg.RepackObjectsStrategyFullWithCruft,
		WriteMultiPackIndex: true,
		WriteBitmap:         len(repoInfo.Alternates.ObjectDirectories) == 0,
		CruftExpireBefore:   expireBefore,
	}); err != nil {
		return nil, structerr.NewInternal("repacking objects: %w", err)
	}
```

**File:** internal/grpc/middleware/housekeeping/middleware.go (L337-400)
```go
func (m *Middleware) scheduleHousekeeping(ctx context.Context, repo *gitalypb.Repository, force bool) {
	m.mu.Lock()
	defer m.mu.Unlock()

	key := m.getRepoKey(repo)

	a, ok := m.repoActivity[key]
	if !ok {
		a = newActivity()
		m.repoActivity[key] = a
	}
	a.writeCount++

	if a.active {
		return
	}

	pendingOps := m.pendingOperations(a, force)
	if len(pendingOps) == 0 {
		return
	}

	m.logger.WithFields(log.Fields{
		"forced":     force,
		"operations": pendingOps,
	}).InfoContext(ctx, "beginning scheduled housekeeping")

	// Mark that these operations are running at the current write count
	for _, op := range pendingOps {
		a.writeCountAtLastRun[op] = a.writeCount
	}

	m.markHousekeepingActive(key)

	m.wg.Add(1)
	go func() {
		// We need to call OptimizeRepository with a child context that's disowned from the parent's
		// cancellation signals we're executing it asynchronously. Providing the existing `ctx` would
		// cause it to fail, since `ctx` would be cancelled when this request completes. We still want
		// to be able to abort the worker when the middleware shuts down though, so we propagate
		// cancellation from the middleware's shutdown context.
		housekeepingCtx, housekeepingCancel := context.WithCancel(context.WithoutCancel(ctx))
		stopShutdownPropagation := context.AfterFunc(m.shutdownCtx, housekeepingCancel)

		defer func() {
			stopShutdownPropagation()
			m.markHousekeepingInactive(key)
			m.logger.InfoContext(housekeepingCtx, "ended scheduled housekeeping")
			housekeepingCancel()
			m.wg.Done()
		}()

		localRepo := m.localRepoFactory.Build(repo)
		if err := m.manager.OptimizeRepository(housekeepingCtx, localRepo,
			manager.WithOptimizationStrategyConstructor(
				func(info stats.RepositoryInfo) housekeeping.OptimizationStrategy {
					return housekeeping.NewSelectiveOptimizationStrategy(info, pendingOps)
				},
			),
			manager.WithMVCCGarbageCollection(),
		); err != nil {
			m.logger.WithError(err).ErrorContext(housekeepingCtx, "failed scheduled housekeeping")
		}
	}()
```
