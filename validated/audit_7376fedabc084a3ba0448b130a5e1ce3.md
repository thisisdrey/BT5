### Title
Unbounded, cost-free repository/object-pool creation enables storage-exhaustion spam attack - ([File: internal/gitaly/service/repository/create_repository.go], [File: internal/gitaly/service/repository/create_fork.go], [File: internal/gitaly/service/objectpool/create.go])

### Summary
The external report describes a Cosmos AMM `CreatePool` method that lacks any creation fee, letting an attacker spam the chain with cheap pools. The closest reachable analog in Gitaly is the family of repository/object-pool "creating RPCs" — `CreateRepository`, `CreateFork`, `CreateRepositoryFromURL/Bundle/Snapshot`, and `CreateObjectPool` — which perform disk-writing operations (git-init/git-clone, module directory creation, repository-counter bookkeeping) without any intrinsic per-call cost, quota, or per-caller limit enforced by Gitaly itself.

### Finding Description
`CreateRepository` validates the request and then calls `repoutil.Create`, which creates a new on-disk repository and increments `s.repositoryCounter`, with no check against any maximum count or size budget. [1](#0-0) 

`CreateFork` similarly performs a full `git clone --bare` fetch of a (potentially large) source repository into a brand-new repository path for every invocation, again with no accounting of cost or per-caller/per-repository throttling beyond generic optional concurrency settings. [2](#0-1) 

`CreateObjectPool` clones a full copy of the origin repository's objects into a new pool repository on disk. [3](#0-2) [4](#0-3) 

These RPCs are explicitly enumerated together as `repositoryCreatingRPCs` in the storage manager, confirming they share the same class of disk-mutating creation operation. [5](#0-4) 

Gitaly's only admission-control mechanism for these RPCs is the optional, operator-configured `[[concurrency]]` limiter, which throttles *concurrent in-flight* calls per RPC/repository but does not cap the *total number* of repositories/pools an authenticated caller can create over time, nor does it impose any cost/fee/quota concept analogous to a "creation fee." [6](#0-5) 
Notably, Gitaly's documentation states that true rate limiting was removed entirely, leaving only concurrency limiting as backpressure: [7](#0-6) 

Because `max_per_repo`/`max_queue_size` limiters key off the *target repository's relative path* (which is unique per new repo/pool), a caller creating N distinct new repositories or pools never collides with itself in the concurrency queue — the limiter provides no defense against sequential or parallel-with-distinct-paths spam. [8](#0-7) 

There is also no repository-count or storage-quota enforcement anywhere in the config or housekeeping code searched (`max_repositories`/quota patterns only appear in cgroups CPU/memory documentation, unrelated to repository counts).

### Impact Explanation
An authenticated Gitaly client (e.g., a compromised or malicious but token-holding CI runner, or any caller with valid `gitaly-auth` credentials as used by GitLab/gitlab-shell) can issue unlimited `CreateRepository`/`CreateFork`/`CreateObjectPool` calls in a loop. Each call permanently consumes disk space, inodes, and an entry in the repository counter/state store, with no fee, quota, or cumulative cost check. At scale this degrades or exhausts storage on the Gitaly node, directly matching the "spamming attack" impact class from the source report (BVSS Availability/Data impact), causing a DoS for legitimate repositories sharing the same storage.

### Likelihood Explanation
Likelihood is moderate-to-high for any deployment that grants broad Gitaly RPC access to less-trusted internal services (e.g., multi-tenant setups, CI systems with SSH/HTTP push access that transitively triggers `CreateFork`/`CreateRepository` via GitLab). The attack requires only valid, ordinary-level authentication to the Gitaly RPC surface — no privilege escalation, leaked admin token, or MITM — and is a pure repeated-call pattern with no rate limiting deployed by default (`[[concurrency]]` is opt-in per-RPC operator config, and even when configured, does not cap distinct-repository creation totals).

### Recommendation
Introduce a creation-cost/quota mechanism analogous to a "pool creation fee": e.g., a per-caller/per-storage maximum repository/object-pool creation rate or absolute count, tracked independently of the per-repository concurrency queue (since each new repo has a unique path and thus never contends with itself). This could be implemented as a dedicated token-bucket/quota check in the `repositoryCreatingRPCs` code path in `middleware.go`, or as a `RepositoryCounter`-backed limit enforced inside `repoutil.Create` before allocating disk resources, with configurable operator-defined ceilings and metrics for visibility.

### Proof of Concept
1. Obtain a valid Gitaly auth token as used by any legitimate but potentially compromised internal caller.
2. Loop invoking `CreateRepository` (or `CreateFork`/`CreateObjectPool`) with a freshly generated unique `RelativePath` on each iteration.
3. Observe that each call succeeds independently — since `LimitConcurrencyByRepo` keys on the (unique, per-call) target path, no concurrency limiter blocks the sequence — and disk usage / repository counter monotonically grows with no server-side ceiling until the storage volume is exhausted. [9](#0-8) [8](#0-7)

### Citations

**File:** internal/gitaly/service/repository/create_repository.go (L15-52)
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

	// When the MVCC backend is requested for new repositories, carry the MVCC reference
	// backend in the context. This selects the MVCC-enabled Git binary for every command
	// run while creating the repository (git-init as well as the subsequent for-each-ref,
	// config and repack commands) and makes git-init use the MVCC ref-format.
	if featureflag.NewRepoMVCCBackend.IsEnabled(ctx) {
		ctx = gitcmd.ContextWithReferenceBackend(ctx, git.ReferenceBackendMVCC)
	}

	if err := repoutil.Create(
		ctx,
		s.logger,
		s.locator,
		s.gitCmdFactory,
		s.catfileCache,
		s.txManager,
		s.repositoryCounter,
		repository,
		func(repo *gitalypb.Repository) error {
			// We do not want to seed the repository with any contents, so we just
			// return directly.
			return nil
		},
		repoutil.WithBranchName(string(req.GetDefaultBranch())),
		repoutil.WithObjectHash(hash),
	); err != nil {
		return nil, structerr.NewInternal("creating repository: %w", err)
	}
```

**File:** internal/gitaly/service/repository/create_fork.go (L16-34)
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

	targetRepository := req.GetRepository()
	sourceRepository := req.GetSourceRepository()

	if err := repoutil.Create(ctx, s.logger, s.locator, s.gitCmdFactory, s.catfileCache, s.txManager, s.repositoryCounter, targetRepository, func(repoProto *gitalypb.Repository) error {
		targetPath, err := s.locator.GetRepoPath(ctx, repoProto, storage.WithRepositoryVerificationSkipped())
		if err != nil {
```

**File:** internal/gitaly/service/objectpool/create.go (L17-42)
```go
func (s *server) CreateObjectPool(ctx context.Context, in *gitalypb.CreateObjectPoolRequest) (*gitalypb.CreateObjectPoolResponse, error) {
	if in.GetOrigin() == nil {
		return nil, errMissingOriginRepository
	}

	poolRepo := in.GetObjectPool().GetRepository()
	if poolRepo == nil {
		return nil, errMissingPool
	}

	if !storage.IsPoolRepository(poolRepo) {
		return nil, errInvalidPoolDir
	}

	// repoutil.Create creates the repositories in a temporary directory. This means the repository is not created in the location
	// expected by the transaction manager. This makes sense without transactions, but with transactions, there's no real point in
	// doing so given a failed transaction's state is anyway removed. Creating the repository in a temporary directory is problematic
	// as the reference transaction hook is invoked for the repository from unexpected location, causing the transaction to fail to
	// associate the reference updates with the repository.
	//
	// Run the repository creation without the transaction in the context. The transactions reads the created repository's state from
	// the disk when committing it, so it's not necessary to capture the updates from the reference-transaction hook. This avoids the
	// problem for now, and later with transactions enabled by default we can stop creating repositories in unexpected locations.
	ctxWithoutTransaction := storage.ContextWithTransactionID(ctx, 0)
	if err := repoutil.Create(ctxWithoutTransaction, s.logger, s.locator, s.gitCmdFactory, s.catfileCache, s.txManager, s.repositoryCounter, poolRepo, func(poolRepo *gitalypb.Repository) error {
		if _, err := objectpool.Create(
```

**File:** internal/git/objectpool/create.go (L60-85)
```go
	var stderr bytes.Buffer
	cmd, err := gitCmdFactory.NewWithoutRepo(ctx,
		gitcmd.Command{
			Name: "clone",
			Flags: []gitcmd.Option{
				gitcmd.Flag{Name: "--quiet"},
				gitcmd.Flag{Name: "--bare"},
				gitcmd.Flag{Name: "--local"},
			},
			Args: []string{sourceRepoPath, objectPoolPath},
		},
		gitcmd.WithRefTxHook(objectHash, sourceRepo),
		gitcmd.WithStderr(&stderr),
		// When cloning an empty repository then Git isn't capable to figure out the correct
		// object hash that the new repository needs to use and just uses the default object
		// format. To work around this shortcoming we thus set the default object hash to
		// match the source repository's object hash.
		gitcmd.WithEnv("GIT_DEFAULT_HASH="+objectHash.Format),
	)
	if err != nil {
		return nil, fmt.Errorf("spawning clone: %w", err)
	}

	if err := cmd.Wait(); err != nil {
		return nil, fmt.Errorf("cloning to pool: %w, stderr: %q", err, stderr.String())
	}
```

**File:** internal/gitaly/storage/storagemgr/middleware.go (L63-71)
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
```

**File:** doc/backpressure.md (L15-24)
```markdown
## Concurrency queue

Limit the number of concurrent RPCs that are in flight on each Gitaly node for each
repository per RPC using `[[concurrency]]` configuration:

```toml
[[concurrency]]
rpc = "/gitaly.SmartHTTPService/PostUploadPackWithSidechannel"
max_per_repo = 1
```
```

**File:** doc/backpressure.md (L54-56)
```markdown
## Note on Rate Limiting

Rate limiting has been removed from Gitaly. For more information about why and the alternatives, please see [issue #5011](https://gitlab.com/gitlab-org/gitaly/-/issues/5011).
```

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
