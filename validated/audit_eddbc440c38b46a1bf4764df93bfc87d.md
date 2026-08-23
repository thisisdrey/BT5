### Title
Missing default concurrency control on `FetchIntoObjectPool` enables repeated expensive `git fetch`/`OptimizeRepository` cycles to exhaust node resources - (File: internal/gitaly/service/objectpool/fetch_into_object_pool.go)

### Summary
`FetchIntoObjectPool` performs an unconditioned `git fetch` from the origin repo into the pool followed by a full `OptimizeRepository` housekeeping pass, with no in-handler locking, deduplication, or default concurrency cap keyed on the target pool. Concurrent calls against the same `ObjectPool`/`Origin` pair are not serialized or throttled by default, so repeated requests each spawn a fresh fetch+repack cycle.

### Finding Description
`FetchIntoObjectPool` validates the request (only checks that origin/pool are non-nil and share a storage) and then directly calls `objectPool.FetchFromOrigin(ctx, origin, ...)` on every invocation, with no guard preventing multiple concurrent invocations from running simultaneously for the same pool: [1](#0-0) 

Inside `FetchFromOrigin`, each call performs stale-data cleanup, a reference-prune pass, a full `git fetch --atomic` from the origin, `git fsck --dangling` to rescue dangling objects, and then triggers `OptimizeRepository` (repack) on the pool: [2](#0-1) 

None of these steps take an exclusive per-pool lock analogous to `repoutil.Lock` used elsewhere (e.g. in repository creation) that would serialize concurrent operations against the same target: [3](#0-2) 

Gitaly's only mechanism to bound concurrent expensive RPCs per repository is the opt-in `[[concurrency]]` middleware configuration, which is disabled by default for arbitrary RPCs. Notably, the code explicitly hard-codes a default concurrency limit of 1 for `ReplicateRepository` because it was recognized as expensive, but no equivalent default exists for `FetchIntoObjectPool`: [4](#0-3)  Administrators must explicitly add a `[[concurrency]] rpc = "/gitaly.ObjectPoolService/FetchIntoObjectPool"` entry to get any protection, which the project's own documentation acknowledges is the primary (and non-default) backpressure mechanism, since rate limiting was intentionally removed from Gitaly: [5](#0-4) 

### Impact Explanation
An attacker able to repeatedly trigger `FetchIntoObjectPool` for a pool they influence (e.g., via repeated housekeeping-triggering actions on a project they control) can fire many concurrent calls, each spawning a `git fetch`, `git fsck`, and full repack/`OptimizeRepository` cycle against the same pool with no default serialization or throttling. On a shared Gitaly node this can exhaust CPU/IO, degrading service for other tenants co-located on the same storage/node — a node-wide availability impact.

### Likelihood Explanation
Feasibility hinges entirely on the assumed precondition that an unprivileged user can cause repeated `FetchIntoObjectPool` invocations against the same pool (the question stipulates this via "repeated housekeeping requests on a project they can influence"). Within Gitaly's own boundary, there is no code-level obstacle: the handler performs no lock acquisition, deduplication, or default rate/concurrency limiting, so any number of parallel calls will each run the full fetch+repack pipeline concurrently.

### Recommendation
Add a per-pool exclusive lock (or single-flight/coalescing) around `FetchIntoObjectPool`/`FetchFromOrigin` similar to `repoutil.Lock`, so concurrent requests against the same pool either queue behind one in-flight fetch+optimize cycle or are rejected/deduplicated. Additionally, consider adding `FetchIntoObjectPool` to the same explicit low-default concurrency limit treatment already applied to `ReplicateRepository` in `WithConcurrencyLimiters`.

### Proof of Concept
```go
// Pseudocode PoC: fire N parallel FetchIntoObjectPool RPCs against the same pool/origin
// and observe that each spawns its own git fetch + OptimizeRepository run rather than
// being serialized or rejected.
var wg sync.WaitGroup
for i := 0; i < N; i++ {
    wg.Add(1)
    go func() {
        defer wg.Done()
        _, err := client.FetchIntoObjectPool(ctx, &gitalypb.FetchIntoObjectPoolRequest{
            Origin:     originRepo,
            ObjectPool: objectPoolProto,
        })
        // expect: all N calls run concurrently, each performing a full
        // fetch/fsck/repack cycle, with no serialization or backoff by default.
        _ = err
    }()
}
wg.Wait()
```
Measuring CPU/IO usage and process counts during this run versus a single call demonstrates that resource consumption scales linearly with N with no built-in cap.

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

**File:** internal/git/objectpool/fetch.go (L31-141)
```go
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

	if err := o.rescueDanglingObjects(ctx); err != nil {
		return fmt.Errorf("rescuing dangling objects: %w", err)
	}

	if err := o.logStats(ctx, "after fetch"); err != nil {
		return fmt.Errorf("computing stats after fetch: %w", err)
	}

	// This RPC fetches new objects from the origin repository into the object pool. Afterward, it
	// triggers a full set of housekeeping tasks. If WAL transaction is enabled, the housekeeping
	// manager initiates a transaction and executes all housekeeping tasks inside the transaction
	// context. Normally, the transaction life cycle is managed by a gRPC middleware. RPC handlers
	// extract the transaction from the context. Unfortunately, following that approach results in
	// two non-nested transactions. The housekeeping transaction is committed before the main
	// fetching one. The housekeeping task's effect is pushed to the next request. That's opposed to
	// the initial intention of running housekeeping after fetching. As a result, this RPC needs to
	// manage the transaction itself so that two transactions can be committed in the right order.
	if tx := storage.ExtractTransaction(ctx); tx != nil {
		commitLSN, err := tx.Commit(ctx)
		if err != nil {
			return fmt.Errorf("commit: %w", err)
		}

		storage.LogTransactionCommit(ctx, o.logger, commitLSN, "FetchFromOrigin")
	}

	// We've committed the original transaction above. OptimizeRepository internally starts
	// another transaction, and knows how to retrieve the original relative path of the repository
	// if there is a transaction in the context.
	if err := o.housekeepingManager.OptimizeRepository(ctx, o.Repo); err != nil {
		return fmt.Errorf("optimizing pool repo: %w", err)
	}

	return nil
```

**File:** internal/gitaly/repoutil/lock.go (L15-55)
```go
// Lock attempts to lock the entire repository directory such that only one
// process can obtain the lock at a time.
//
// The repositories parent directory will be created if it does not exist.
//
// Returns the error safe.ErrFileAlreadyLocked if the repository is already
// locked.
func Lock(ctx context.Context, logger log.Logger, locator storage.Locator, repository storage.Repository) (func(), error) {
	path, err := locator.GetRepoPath(ctx, repository, storage.WithRepositoryVerificationSkipped())
	if err != nil {
		return nil, err
	}

	// Create the parent directory in case it doesn't exist yet.
	if err := os.MkdirAll(filepath.Dir(path), mode.Directory); err != nil {
		return nil, structerr.NewInternal("create directories: %w", err)
	}

	// We're somewhat abusing this file writer given that we simply want to assert that
	// the target directory doesn't exist and isn't created while we want to move the
	// new repository into place. We thus only use the locking semantics of the writer,
	// but will never commit it.
	locker, err := safe.NewLockingFileWriter(path)
	if err != nil {
		return nil, err
	}

	unlock := func() {
		if err := locker.Close(); err != nil {
			logger.WithError(err).ErrorContext(ctx, "closing repository locker failed")
		}
	}

	if err := locker.Lock(); err != nil {
		unlock()

		return nil, err
	}

	return unlock, nil
}
```

**File:** internal/grpc/middleware/limithandler/middleware.go (L280-293)
```go
		// Set default for ReplicateRepository.
		replicateRepositoryFullMethod := "/gitaly.RepositoryService/ReplicateRepository"
		if _, ok := result[replicateRepositoryFullMethod]; !ok {
			result[replicateRepositoryFullMethod] = limiter.NewConcurrencyLimiter(
				limiter.NewAdaptiveLimit("staticLimit", limiter.AdaptiveSetting{Initial: 1}),
				0,
				0,
				limiter.NewPerRPCPromMonitor(
					"gitaly", replicateRepositoryFullMethod,
					queuedMetric, inProgressMetric, acquiringSecondsMetric, middleware.requestsDroppedMetric,
					true,
				),
			)
		}
```

**File:** doc/backpressure.md (L13-56)
```markdown
We employ concurrency limiting as our primary backpressure mechanism in Gitaly.

## Concurrency queue

Limit the number of concurrent RPCs that are in flight on each Gitaly node for each
repository per RPC using `[[concurrency]]` configuration:

```toml
[[concurrency]]
rpc = "/gitaly.SmartHTTPService/PostUploadPackWithSidechannel"
max_per_repo = 1
```

For example:

- One clone request comes in for repository "A" (a largish repository).
- While this RPC is executing, another request comes in for repository "A". Because
  `max_per_repo` is 1 in this case, the second request blocks until the first request
  is finished.

An in-memory queue of requests can build up in Gitaly that are waiting their turn. Because
this is a potential vector for a memory leak, two other values in the `[[concurrency]]`
configuration can prevent an unbounded in-memory queue of requests:

- `max_queue_wait` is the maximum amount of time a request can wait in the
  concurrency queue. When a request waits longer than this time, it returns
  an error to the client.
- `max_queue_size` is the maximum size the concurrency queue can grow for a
  given RPC. If a concurrency queue is at its maximum, subsequent requests
  return with an error. The queue size is per repository.

For example:

```toml
[[concurrency]]
rpc = "/gitaly.SmartHTTPService/PostUploadPackWithSidechannel"
max_per_repo = 1
max_queue_wait = "1m"
max_queue_size = 5
```

## Note on Rate Limiting

Rate limiting has been removed from Gitaly. For more information about why and the alternatives, please see [issue #5011](https://gitlab.com/gitlab-org/gitaly/-/issues/5011).
```
