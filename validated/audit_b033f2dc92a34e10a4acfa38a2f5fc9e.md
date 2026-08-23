### Title
Missing existence/validity validation of the `origin` repository in `FetchIntoObjectPool` allows cross-repository object fetches into a shared object pool - (File: internal/gitaly/service/objectpool/fetch_into_object_pool.go)

### Summary
`FetchIntoObjectPoolRequest.Origin` is only checked for nilness and for matching `StorageName` with the object pool, never passed through `storage.Locator.ValidateRepository`. This mirrors the Aave `H03` pattern where the "collateral" reserve argument was never checked to be active while the primary reserve was: here the "target"/object-pool repository undergoes locator validation via `objectpool.FromProto`, but the secondary repository argument (`origin`) that is subsequently used to perform a privileged git operation (a fetch into the object pool) skips that validation entirely.

### Finding Description
`FetchIntoObjectPool` validates the object pool via `objectpool.FromProto`, which internally resolves and checks pool repository state, but the `origin` argument is validated only by `validateFetchIntoObjectPoolRequest`, which checks for nil and that the storage names match — it never calls `s.locator.ValidateRepository` on `origin`, unlike other RPCs (`LinkRepositoryToObjectPool`, `CreateFork`, `Replicate`) that explicitly validate every repository argument they accept. [1](#0-0) [2](#0-1) 

`origin` is then built directly into a `localrepo.Repo` and handed to `objectPool.FetchFromOrigin`, which performs the actual `git fetch` from that repository's path into the pool's object database: [3](#0-2) 

By contrast, `LinkRepositoryToObjectPool` explicitly validates the (analogous) repository argument before use: [4](#0-3) 

Because `origin` skips `ValidateRepository`, the RPC accepts any storage-name/relative-path pair on the same storage without confirming it is an existing, valid Git directory. Since the object pool is shared as an alternates source across every fork/member linked to it, an unvalidated `origin` value effectively lets an attacker who controls the `origin` field of this internal RPC pull objects from an arbitrary relative path into a pool object database that other repositories treat as a trusted alternate, which is the same class of issue as the original report: a secondary repository argument that should be gated the same way as the primary one is not, and its effects (writing objects into a pool shared by many repos) are silently unaudited for that argument.

### Impact Explanation
If reachable with an attacker-influenced `origin` value, this allows objects from a repository outside of the intended fork network to be pulled into a shared object pool, i.e., a form of cross-repository object access/contamination, since the object pool's alternates are subsequently visible to every repository linked to it. It also means Gitaly performs a potentially expensive/side-effecting git operation (`git fetch`) against a repository path that was never confirmed to be a valid Git repository, which can also serve as a DoS/error-oracle vector for enumerating storage layout via error messages if `origin` points to a bogus path.

### Likelihood Explanation
`FetchIntoObjectPoolRequest` is an internal Gitaly RPC (`ObjectPoolService`), typically invoked by Gitaly's own replication/coordination code or via Praefect routing rather than being directly exposed to end users; I could not fully verify from the available index whether an ordinary user-driven push/fetch/fork flow can control the `Origin` field's value end-to-end (this needs verification against the Rails/GitLab caller and Praefect routing code, which is only partially covered by the codebase index). Given this uncertainty about caller-side sanitization, likelihood should be treated as moderate rather than confirmed.

### Recommendation
Add an explicit `s.locator.ValidateRepository(ctx, req.GetOrigin())` check (mirroring what `LinkRepositoryToObjectPool` and other repository-accepting RPCs already do) inside `validateFetchIntoObjectPoolRequest` before `FetchFromOrigin` is invoked, ensuring the origin repository is confirmed to exist and be a valid Git repository on the expected storage prior to being fetched from.

### Proof of Concept
Not fully constructible from the indexed code alone: reproducing the issue requires confirming (a) that `FetchIntoObjectPoolRequest.Origin` can be set to an arbitrary/unauthorized relative path by a caller reachable from user-triggered flows, and (b) that `FetchFromOrigin`'s underlying git-fetch command does not perform its own equivalent existence/validity check. Given the index's coverage of `internal/git/objectpool/fetch.go` was not retrievable in this pass, this last point could not be verified with certainty. A Devin session with full repository access should confirm `FetchFromOrigin`'s implementation and the call sites that populate `Origin` in `FetchIntoObjectPoolRequest` before treating this as conclusively exploitable.

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

**File:** internal/gitaly/service/objectpool/link.go (L10-19)
```go
func (s *server) LinkRepositoryToObjectPool(ctx context.Context, req *gitalypb.LinkRepositoryToObjectPoolRequest) (*gitalypb.LinkRepositoryToObjectPoolResponse, error) {
	repository := req.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repository); err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}

	pool, err := s.poolForRequest(ctx, req)
	if err != nil {
		return nil, err
	}
```
