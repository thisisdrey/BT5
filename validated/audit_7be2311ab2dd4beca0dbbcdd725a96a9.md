### Title
Permissionless accessor RPCs permanently disable stat-threshold-based housekeeping via a one-shot process-lifetime cache - ([File: internal/grpc/middleware/housekeeping/middleware.go])

### Summary
`scheduleHousekeepingIfNeeded` computes a "live" measurement of repository state (file/directory count) from an ordinary, unprivileged accessor RPC, and uses a permissionless, one-time write to a process-lifetime cache to decide whether the stat-threshold housekeeping safety net ever runs again for that repository — structurally the same pattern as the Balancer bug: a mutable, attacker-influenceable live measurement is captured and permanently locked into a decision gate by an unprivileged caller, at a time of the attacker's choosing, bypassing the intended threshold protection for the remaining lifetime of the process.

### Finding Description
`scheduleHousekeepingIfNeeded` is invoked from `UnaryServerInterceptor`/`StreamServerInterceptor` for every `OpAccessor` RPC (i.e., ordinary read/fetch-path calls that any authorized client can issue), not just privileged maintenance RPCs: [1](#0-0) 

The function walks the repository directory to gather a live `file+directory` count, and — the very first time it runs for a given `repoKey` — permanently marks that key as "checked" in a `sync.Map`, regardless of the outcome: [2](#0-1) 

Because the check-and-store happens unconditionally on the *first* accessor call for a repo, and an ordinary user fully controls when they first fetch/browse a freshly created (small) repository, they can trivially "lock in" a low stat count before subsequent large pushes, imports, or replication traffic grow the repository. The docstring itself concedes the design intent ("a single stats check per restart is sufficient") but the implementation makes that single check permanent and irreversible for the life of the Gitaly process, with no re-evaluation as the repository grows: [3](#0-2) 

This mirrors the Balancer report's core defect: a value derived from current, attacker-influenceable state (`getPrice()` reading live vault balances / here, live directory walk) is captured via a permissionless call (`updatePrice()` / here, the first accessor RPC) into a stored decision (`snapshot.averagePrice` / here, `statsCache` "checked" marker) that later gates a protective mechanism (`validatePrice()` slippage check / here, `operationsExceedingStatThreshold` housekeeping trigger). In both cases the attacker chooses the timing to steer the stored state into a permanently favorable regime.

### Impact Explanation
This disables one of the two independent triggers for stat-threshold-based housekeeping (`OpRepackRefs`, `OpRepackObjects`, `OpPruneObjects`, `OpWriteCommitGraph`) for repositories whose growth is not solely driven by RPCs classified as `OpMutator` that go through `scheduleHousekeeping`'s `RPCInterval` counter — e.g., pool/fork repositories, replicated repositories, or repositories whose object/ref count balloons from operations outside the mutator write-count model. For those, the accessor-triggered stat threshold check (meant specifically as the fallback for "repositories that don't trigger housekeeping through write operations") never fires again once any client has issued a single early accessor call, allowing unbounded accumulation of loose objects/refs/directories without the intended automatic repack/prune, degrading storage usage and RPC performance over the life of the Gitaly process (resource-limit/DoS class impact on a handler-gating mechanism).

### Likelihood Explanation
Any authorized user who can call an accessor RPC (e.g. a simple read/fetch operation right after repository creation) triggers this path with no special privilege required, and the race to be "first" is trivial to win since repositories are naturally small immediately after creation. However, severity is bounded because the primary, independent mutator-driven scheduling path (`scheduleHousekeeping` via `RPCInterval`) remains active and unaffected, so the impact is limited to repositories/growth patterns that rely specifically on the accessor-triggered stat fallback.

### Recommendation
Do not use a single permanent per-process "checked" flag gated by an unprivileged, attacker-timeable RPC to decide whether the stat-threshold safety net ever runs. Instead, periodically re-evaluate the stat threshold (e.g., with a TTL/expiry, or by re-checking on a cadence rather than once-ever), or tie invalidation of `statsCache` to actual repository mutation events so that growth after the initial check is still evaluated.

### Proof of Concept
1. Create a new repository (small file/dir count).
2. As an ordinary client, issue any `OpAccessor` RPC (e.g. `TreeEntry`, `FindCommit`) against the repository immediately — this calls `scheduleHousekeepingIfNeeded`, which computes a low `totalCount`, finds no `operationsExceedingStatThreshold`, and unconditionally stores the key in `statsCache`. [4](#0-3) 
3. Grow the repository well beyond `StatThreshold` (default 1000 files+dirs, or 500 for `OpRepackRefs`) via operations that don't sufficiently trip the independent `RPCInterval` mutator counter (e.g., a small number of large mutator calls each adding many refs/objects, or non-`OpMutator`-tracked growth such as replication).
4. Issue further `OpAccessor` RPCs against the same repository — `scheduleHousekeepingIfNeeded` short-circuits on `statsCache.Load(key)` and never re-walks or re-evaluates the threshold for the remaining lifetime of the process, confirming the stat-based housekeeping fallback is permanently disabled for that repository. [5](#0-4)

### Citations

**File:** internal/grpc/middleware/housekeeping/middleware.go (L202-204)
```go
		case protoregistry.OpAccessor:
			m.scheduleHousekeepingIfNeeded(ctx, key, targetRepo)
		}
```

**File:** internal/grpc/middleware/housekeeping/middleware.go (L403-439)
```go
// scheduleHousekeepingIfNeeded walks the repository path to gather file and directory counts,
// and schedules housekeeping if the total count exceeds the configured threshold.
// Uses a sync.Map cache to track repositories where statistics have been calculated, ensuring
// the calculation occurs only once per application restart. This targets accessor RPCs for
// repositories that don't trigger housekeeping through write operations, where a single stats
// check per restart is sufficient for low-activity repos.
func (m *Middleware) scheduleHousekeepingIfNeeded(ctx context.Context, key repoKey, targetRepo *gitalypb.Repository) {
	if _, statsChecked := m.statsCache.Load(key); !statsChecked {
		localRepo := m.localRepoFactory.Build(targetRepo)
		repositoryPath, err := localRepo.Path(ctx)
		if err != nil {
			if errors.Is(err, storage.ErrRepositoryNotFound) {
				return
			}
			m.logger.WithError(err).ErrorContext(ctx, "housekeeping: find repo path")
			return
		}

		repoStats := snapshot.RepositoryStatistics{}
		if err := snapshot.WalkPathForStats(ctx, repositoryPath, &repoStats); err != nil {
			m.logger.WithError(err).ErrorContext(ctx, "calculate repository statistics")
			return
		}

		m.logger.WithFields(log.Fields{
			"repository_stats": map[string]any{
				"directory_count": repoStats.DirectoryCount,
				"file_count":      repoStats.FileCount,
			},
		}).InfoContext(ctx, "collected repository statistics")

		m.statsCache.Store(key, struct{}{})
		totalCount := repoStats.DirectoryCount + repoStats.FileCount
		ops := m.operationsExceedingStatThreshold(totalCount)
		if len(ops) > 0 {
			m.scheduleHousekeeping(ctx, targetRepo, true)
		}
```
