Based on the codebase evidence, I found a genuine unrestricted-repeated-invocation analog in Gitaly's repository-creation RPCs.

### Title
Unbounded repository/object-pool creation via per-repository concurrency keying allows resource-exhaustion spam - ([File: internal/grpc/middleware/limithandler/middleware.go])

### Summary
Gitaly's only backpressure mechanism against RPC spam is the optional, admin-configured `[[concurrency]]` limiter, which is keyed **per target repository path** [1](#0-0) . For repository-creating RPCs such as `CreateFork`, `CreateRepositoryFromURL`, `CreateRepository`, and `CreateObjectPool`, every call targets a brand-new, distinct `relative_path` supplied by the caller [2](#0-1) . Because the lock/queue key is the (always-unique) target repository path rather than the caller identity or storage, the per-repo concurrency limit never accumulates across calls, so an authenticated but otherwise ordinary user who is permitted to create repositories/forks can invoke these mutator RPCs an unbounded number of times in parallel with no queueing, throttling, or global cap — mirroring the "public function with cheap-to-satisfy checks, spammable without restriction" pattern in the referenced report, except the effect here is storage/RPC resource exhaustion rather than event-log spam.

### Finding Description
- `repositoryCreatingRPCs` explicitly lists `CreateObjectPool`, `CreateFork`, `CreateRepository`, `CreateRepositoryFromURL`, `CreateRepositoryFromBundle`, `CreateRepositoryFromSnapshot`, and `ReplicateRepository` as RPCs that create a new repository [3](#0-2) .
- The concurrency limiter's default lock key function, `LimitConcurrencyByRepo`, derives its key from `info.Repository.GetRelativePath()` of the request's target repository [1](#0-0) .
- Handlers such as `CreateFork` and `CreateRepository` never validate or cap how many *new* repositories a given caller/session may create; each call simply validates the (non-existent) target path and proceeds to clone/init it [2](#0-1) [4](#0-3) .
- Gitaly's own documentation confirms that rate limiting (a cross-request, identity/storage-scoped mechanism) was intentionally removed, leaving only the per-repo concurrency queue as backpressure [5](#0-4) .
- The only RPC in this creating-set that gets a hard-coded default concurrency limit is `ReplicateRepository` (capped at 1) [6](#0-5) ; `CreateFork`, `CreateRepository`, `CreateRepositoryFromURL`, and `CreateObjectPool` have no such default, and even if an operator configures `max_per_repo` for them, the unique-relative-path-per-call semantics render that setting ineffective against a caller who varies the target path on every call.

### Impact Explanation
An authenticated actor with ordinary repository/fork-creation privileges (the same privilege GitLab already grants to any project member/importer) can flood a Gitaly node with concurrent `CreateFork`/`CreateRepositoryFromURL`/`CreateObjectPool` calls, each targeting a new unique path. Because the concurrency limiter cannot meaningfully throttle this pattern, the requests are not queued against each other and proceed in full parallel, consuming disk I/O, inodes, storage quota, git-clone subprocess slots, and (per `repoutil.Create`) transactional vote/lock overhead for every new repository [7](#0-6) . This is a resource-exhaustion DoS against the storage/Gitaly node, potentially degrading service for all repositories co-located on that storage.

### Likelihood Explanation
High for any tenant/user who already has legitimate create/fork permission (a very low bar in GitLab-backed deployments — any user permitted to fork a public/internal project or import a repository can trigger `CreateFork`/`CreateRepositoryFromURL`). No malicious peer, leaked token, or privileged role is required — it only needs an ordinary, valid, already-authorized actor issuing many concurrent/rapid calls with distinct target paths, which is straightforward to script.

### Recommendation
Add a caller/storage-scoped (not solely target-repository-path-scoped) admission control for the repository-creating RPC set (`CreateFork`, `CreateRepository`, `CreateRepositoryFromURL`, `CreateRepositoryFromBundle`, `CreateRepositoryFromSnapshot`, `CreateObjectPool`), e.g., a per-identity or per-storage concurrency/queue limiter in addition to (or instead of) the per-repository-path key, and/or a default sane concurrency cap analogous to the one already hard-coded for `ReplicateRepository` [6](#0-5) .

### Proof of Concept
1. Obtain a valid Gitaly auth token / GitLab-authorized session that is permitted to fork or create repositories on a target storage.
2. Script N concurrent `CreateFork` (or `CreateRepositoryFromURL`) gRPC calls, each with a freshly generated unique `relative_path` for the target `Repository` (e.g., `gittest.NewRepositoryName`-style random paths) and any accessible `source_repository`/`url`.
3. Observe that the per-repo concurrency limiter (`LimitConcurrencyByRepo`) never triggers, because `info.Repository.GetRelativePath()` differs on every call, so no queueing/backoff occurs even with `[[concurrency]]` configured for these RPCs.
4. Observe unbounded parallel `git clone`/`git init` subprocess spawning and disk usage growth on the storage backing the target, degrading node performance/exhausting storage quota.

### Citations

**File:** internal/grpc/middleware/limithandler/middleware.go (L18-25)
```go
// LimitConcurrencyByRepo implements GetLockKey by using the repository path as lock.
func LimitConcurrencyByRepo(ctx context.Context) string {
	if info := requestinfohandler.Extract(ctx); info != nil {
		return info.Repository.GetRelativePath()
	}

	return ""
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

**File:** internal/gitaly/service/repository/create_fork.go (L16-27)
```go
func (s *server) CreateFork(ctx context.Context, req *gitalypb.CreateForkRequest) (*gitalypb.CreateForkResponse, error) {
	// We don't validate existence of the source repository given that we may connect to a different Gitaly host in
	// order to fetch from it. So it may or may not exist locally.
	if err := s.locator.ValidateRepository(ctx, req.GetSourceRepository(), storage.WithSkipStorageExistenceCheck()); err != nil {
		return nil, structerr.NewInvalidArgument("validating source repository: %w", err)
	}

	// Neither do we validate existence of the target repository given that this is the repository we wish to create
	// in the first place.
	if err := s.locator.ValidateRepository(ctx, req.GetRepository(), storage.WithSkipRepositoryExistenceCheck()); err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}
```

**File:** internal/gitaly/storage/storagemgr/middleware.go (L63-72)
```go
// repositoryCreatingRPCs are all of the RPCs that may create a repository.
var repositoryCreatingRPCs = map[string]struct{}{
	gitalypb.ObjectPoolService_CreateObjectPool_FullMethodName:             {},
	gitalypb.RepositoryService_CreateFork_FullMethodName:                   {},
	gitalypb.RepositoryService_CreateRepository_FullMethodName:             {},
	gitalypb.RepositoryService_CreateRepositoryFromURL_FullMethodName:      {},
	gitalypb.RepositoryService_CreateRepositoryFromBundle_FullMethodName:   {},
	gitalypb.RepositoryService_CreateRepositoryFromSnapshot_FullMethodName: {},
	gitalypb.RepositoryService_ReplicateRepository_FullMethodName:          {},
}
```

**File:** internal/gitaly/service/repository/create_repository.go (L15-24)
```go
func (s *server) CreateRepository(ctx context.Context, req *gitalypb.CreateRepositoryRequest) (*gitalypb.CreateRepositoryResponse, error) {
	repository := req.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repository, storage.WithSkipRepositoryExistenceCheck()); err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}

	hash, err := git.ObjectHashByProto(req.GetObjectFormat())
	if err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}
```

**File:** doc/backpressure.md (L54-56)
```markdown
## Note on Rate Limiting

Rate limiting has been removed from Gitaly. For more information about why and the alternatives, please see [issue #5011](https://gitlab.com/gitlab-org/gitaly/-/issues/5011).
```

**File:** internal/gitaly/repoutil/create.go (L178-196)
```go
	// We're now entering the critical section where we want to have exclusive access
	// over creation of the repository. So we:
	//
	// 1. Lock the repository path such that no other process can create it at the same
	//    time.
	// 2. Vote on the new repository's state.
	// 3. Move the repository into place.
	// 4. Do another confirmatory vote to signal that we performed the change.
	// 5. Unlock the repository again.
	//
	// This sequence guarantees that the change is atomic and can trivially be rolled
	// back in case we fail to either lock the repository or reach quorum in the initial
	// vote.
	unlock, err := Lock(ctx, logger, locator, repository)
	if err != nil {
		return fmt.Errorf("locking repository: %w", err)
	}
	defer unlock()

```
