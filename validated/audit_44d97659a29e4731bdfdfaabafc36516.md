### Title
Per-repository concurrency limiting can be circumvented by fanning requests across object-pool fork members - (File: `internal/grpc/middleware/limithandler/middleware.go`)

### Summary
Gitaly's concurrency limiter keys its per-repository throttling on the *requesting* repository's own relative path, ignoring that many repositories can share the same underlying object storage through Git object pools. An attacker who controls (or simply uses) several fork repositories linked to one object pool can spread concurrently-issued expensive RPCs across those distinct relative-path keys, each individually staying under the configured per-repo limit, while collectively hammering the single shared pool far beyond the limit's intended bound — mirroring the report's pattern of using multiple accounts to defeat a per-account limit.

### Finding Description
`LimitConcurrencyByRepo` derives the limiter's lock key strictly from the target repository's own relative path: [1](#0-0) 

This key is used to bucket per-repository concurrency limits configured via `cfg.Concurrency` (`MaxPerRepo`, queueing, etc.): [2](#0-1) 

However, Git object pools let multiple, independently-named repositories (fork "members") share the same physical object storage via an `objects/info/alternates` file pointing at a common pool repository: [3](#0-2) 

Linking establishes this alternate relationship per member without any coupling back into the concurrency-limiter's key space: [4](#0-3) 

Because the limiter's lock key is the calling repository's own relative path (not the pool's), the per-repo concurrency limit only bounds concurrency *per fork*, never concurrency against the pool object storage they all share. A single physical resource (the pool's `objects/` directory, ODB reads, delta base I/O, disk/page-cache pressure) can therefore receive concurrency far beyond any single `MaxPerRepo` setting simply by distributing requests across N distinct fork relative paths — exactly the "several accounts, same underlying resource, individually-valid checks" pattern described in the report, where withdrawal-window compliance was checked per-account while the underlying balance/resource moved freely between accounts.

### Impact Explanation
This allows an authenticated but otherwise unprivileged user who owns or creates multiple forks of the same upstream (or otherwise gets many repositories linked into one pool) to defeat the operator's per-repository concurrency protection and drive unbounded concurrent load (CPU, I/O, memory) against the shared pool repository — a DoS vector against a resource whose protection was explicitly intended to be bounded by the concurrency limiter. This is a genuine gap between the limiter's threat model (per-repo isolation) and the actual storage topology (shared object pools), reachable purely through ordinary fork creation and normal RPC traffic (e.g., repeated reads/clones/fetches on many pool members at once).

### Likelihood Explanation
Likelihood is moderate-to-high: creating multiple forks of a project and linking them to the same pool is a normal, unprivileged GitLab workflow (`CreateFork`/`CreateObjectPool`/`LinkRepositoryToObjectPool`), requiring no special access. An attacker only needs enough forks (or access to several existing pool members) to multiply the effective concurrency against the pool beyond the configured `MaxPerRepo` value; the more fork members available, the more the limiter's guarantee degrades, with no cap accounting for shared alternates.

### Recommendation
When computing the limiter's lock key (`LimitConcurrencyByRepo`), resolve the repository to its object pool (if `objects/info/alternates` links it to one, as detected in `internal/gitaly/storage/storagemgr/middleware.go`'s partitioning logic) and use the pool's relative path — or a combined pool+repo key with an additional pool-wide limit — so concurrency against the shared object store is bounded regardless of how many distinct fork relative paths are used to reach it.

### Proof of Concept
1. Create an object pool from repository `A` and link N fork repositories `A1..AN` to it via `LinkRepositoryToObjectPool`, as described in `doc/object_pools.md`.
2. Configure a `MaxPerRepo` concurrency limit (e.g., `2`) for an expensive RPC (e.g., a blob/commit read RPC that reads through alternates) in `cfg.Concurrency`.
3. Issue `2` concurrent requests against each of `A1..AN` simultaneously (instead of against a single repository).
4. Observe that the limiter admits `2*N` concurrent operations that all resolve through the shared pool's object directory — exceeding the intended `MaxPerRepo=2` protection on the underlying shared resource, because `LimitConcurrencyByRepo` (`internal/grpc/middleware/limithandler/middleware.go:18-25`) keys admission per fork's relative path rather than per shared pool.

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

**File:** internal/grpc/middleware/limithandler/middleware.go (L74-97)
```go
// UnaryInterceptor returns a Unary Interceptor
func (c *LimiterMiddleware) UnaryInterceptor() grpc.UnaryServerInterceptor {
	return func(ctx context.Context, req interface{}, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (interface{}, error) {
		lockKey := c.getLockKey(ctx)
		if lockKey == "" {
			return handler(ctx, req)
		}

		// Check if request is authenticated
		limiter := c.methodLimiters[info.FullMethod]
		unauthLimiter, ok := c.methodLimitersUnauthenticated[info.FullMethod]
		if !auth.IsAuthenticated(ctx) && ok {
			limiter = unauthLimiter
		}

		if limiter == nil {
			// No concurrency limiting
			return handler(ctx, req)
		}

		return limiter.Limit(ctx, lockKey, func() (interface{}, error) {
			return handler(ctx, req)
		})
	}
```

**File:** doc/object_pools.md (L1-12)
```markdown
# Object Pools

When creating forks of a repository, most of the objects for forked repository
and the repository it forked from are shared. Storing those shared objects
multiple times is a waste of disk space and also of CPU time, given that those
shared objects would have to be repacked for both repositories. To fix this
waste of resources, we use object pools, which are essentially a repository
which holds the shared objects of both repositories.

The sharing of objects for a given repository and its object pool is done via
alternate object directories which Gitaly sets up when linking a repository to
an object pool by writing the `objects/info/alternates` file.
```

**File:** internal/git/objectpool/link.go (L25-66)
```go
// Link will link the given repository to the object pool. This is done by writing the object pool's
// path relative to the repository into the repository's "alternates" file. This does not trigger
// deduplication, which is the responsibility of the caller.
func Link(ctx context.Context, pool, repo *localrepo.Repo, txManager transaction.Manager) (returnedErr error) {
	altPath, err := repo.InfoAlternatesPath(ctx)
	if err != nil {
		return err
	}

	expectedRelPath, err := getRelativeObjectPath(ctx, pool, repo)
	if err != nil {
		return err
	}

	linked, err := linkedToRepository(ctx, pool, repo)
	if err != nil {
		return err
	}

	if linked {
		// When the repository is already linked to the repository, cast a vote to ensure the
		// repository is consistent with the other replicas.
		if err := transaction.VoteOnContext(ctx, txManager, voting.VoteFromData([]byte("repository linked")), voting.Synchronized); err != nil {
			return fmt.Errorf("vote on linked repository: %w", err)
		}

		return nil
	}

	alternatesWriter, err := safe.NewLockingFileWriter(altPath)
	if err != nil {
		return fmt.Errorf("creating alternates writer: %w", err)
	}
	defer func() {
		if err := alternatesWriter.Close(); err != nil && returnedErr == nil {
			returnedErr = fmt.Errorf("closing alternates writer: %w", err)
		}
	}()

	if _, err := io.WriteString(alternatesWriter, expectedRelPath); err != nil {
		return fmt.Errorf("writing alternates: %w", err)
	}
```
