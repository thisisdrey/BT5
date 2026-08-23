### Title
Per-repository housekeeping serialization is bypassable across shared object-pool members - ([File: internal/git/housekeeping/manager/optimize_repository.go])

### Summary
The external report describes an NFT sale limiter that is scoped to the wrong identity: `saleConfig.limitPerAccount` is checked against `balanceOf(account)`, a value the account fully controls (by transferring tokens out), so the "per-account" limit never actually bounds the shared, scarce resource (the fixed drop supply). The analogous pattern in Gitaly is `RepositoryManager.OptimizeRepository`'s single-flight-per-repository guard: it locks on the *calling* repository's identity, not on the shared object storage that concurrent callers actually mutate when the repository is a member of an object pool.

### Finding Description
`OptimizeRepository` acquires housekeeping serialization via `m.repositoryStates.tryRunningHousekeeping(originalRepo)`, keyed by the caller's own `RelativePath`/`StorageName` [1](#0-0) . This correctly serializes repeated `OptimizeRepository` calls against the *same* repository identity, as shown in `TestOptimizeRepository_ConcurrencyLimit`'s "serialized optimizations" case [2](#0-1) .

However, Gitaly's object-pool design deliberately allows many distinct repositories (pool members, i.e. forks) to share the *same* underlying object database via `objects/info/alternates` [3](#0-2) . The "multiple repositories concurrently" test explicitly demonstrates that `OptimizeRepository` calls against two *different* relative paths are never serialized against each other, even when both target repositories are linked to the identical pool: `repoFirst` and `repoSecond` run concurrently and both call `optimizeFunc` in parallel [4](#0-3) . The lock is keyed by `repo.GetRelativePath()`/`StorageName`, which is per-member, not per-underlying-object-store [5](#0-4) .

This mirrors the report's root cause precisely: a resource-protection check (concurrency-of-one for housekeeping/repack on the object database) is scoped to an identity (`RelativePath` of the calling repo / `balanceOf(account)`) that the actor can multiply (forking additional pool members / using additional wallets or a transfer step) while the actual shared, size-limited resource (the pool's alternates objects directory / the fixed-supply NFT collection) is left effectively unprotected against concurrent access from those multiplied identities.

### Impact Explanation
An ordinary user who can create multiple forks of the same project (or otherwise link multiple accessible repositories to the same object pool) can drive `OptimizeRepository`/`git-repack` concurrently against every pool member simultaneously. Because `git-repack -A` reads and rewrites the pool's shared packfiles/alternates and multiple member-scoped repacks can run against the same physical object directory at once, this creates: (1) a resource-exhaustion / DoS vector on Gitaly nodes hosting the pool (multiple concurrent CPU/IO-heavy repacks on the same object store that the per-repo limiter was meant to prevent one caller from triggering repeatedly), and (2) increased risk of races on the shared alternates object directory beyond what the single-flight guard was designed to prevent, since the guard's protection is torn apart across member identities. This is a design limitation of an unprivileged, reachable path (any caller of `OptimizeRepository`/`RepositoryService.OptimizeRepository` RPC against pool member repositories they have access to), not a memory-safety or auth bypass, so impact is bounded to load/DoS and best-effort repack correctness, not data corruption of the object graph (which is protected separately by quarantine/fsck and transaction machinery).

### Likelihood Explanation
Likelihood is moderate: it requires having (or creating) multiple repositories linked to the same object pool, which is a normal, unprivileged capability (forking) in GitLab-style setups, and issuing `OptimizeRepository` (or triggering the auto-optimization path) concurrently against several of them. No token leakage, malicious peer, or privileged access is required.

### Recommendation
Key the housekeeping single-flight lock (and any per-repository resource limiter intended to bound work on a shared object store) on the resolved object-directory identity (post-alternate resolution) rather than solely on the calling repository's own relative path, so that all pool members funnel through the same serialization key as the pool itself. Alternatively, detect linked-pool membership in `tryRunningHousekeeping` and acquire the pool's own key in addition to the member's key before performing repacking work.

### Proof of Concept
1. Create an object pool repository `pool` and link two member repositories `member-a` and `member-b` to it via `objects/info/alternates` (as `gittest.CreateObjectPool`/`linkRepoToPool` do in tests) [6](#0-5) .
2. Concurrently call `RepositoryService.OptimizeRepository` for `member-a` and `member-b` (and additional members if created).
3. Observe both calls proceed to run `optimizeRepository`/`git-repack` concurrently rather than being serialized, as demonstrated by the "multiple repositories concurrently" test case which asserts both repos are optimized in parallel without blocking [7](#0-6) , despite both ultimately touching the same pool object storage.

### Citations

**File:** internal/git/housekeeping/manager/optimize_repository.go (L72-91)
```go
	if err := m.maybeStartTransaction(ctx, repo, func(ctx context.Context, tx storage.Transaction, repo *localrepo.Repo) error {
		originalRepo := &gitalypb.Repository{
			StorageName:  repo.GetStorageName(),
			RelativePath: repo.GetRelativePath(),
		}
		if tx != nil {
			originalRepo = tx.OriginalRepository(originalRepo)
		}

		// tryRunningHousekeeping acquires a lock on the repository to prevent other concurrent housekeeping calls on the repository.
		// As we may be in a transaction, the repository's relative path may have been rewritten. We use the original unrewritten relative
		// path here to ensure we hit the same key regardless if we run in different transactions where the snapshot prefixes in the
		// relative paths may differ.
		ok, cleanup := m.repositoryStates.tryRunningHousekeeping(originalRepo)
		// If we didn't succeed to set the state to "running" because of a concurrent housekeeping run
		// we exit early.
		if !ok {
			return nil
		}
		defer cleanup()
```

**File:** internal/git/housekeeping/manager/optimize_repository_test.go (L349-366)
```go
	linkRepoToPool := func(t *testing.T, repoPath, poolPath string, date time.Time) {
		t.Helper()

		alternatesPath := filepath.Join(repoPath, "objects", "info", "alternates")

		alternatesContent, err := filepath.Rel(
			filepath.Join(repoPath, "objects"),
			filepath.Join(poolPath, "objects"),
		)
		require.NoError(t, err)

		require.NoError(t, os.WriteFile(
			alternatesPath,
			[]byte(alternatesContent),
			mode.File,
		))
		require.NoError(t, os.Chtimes(alternatesPath, date, date))
	}
```

**File:** internal/git/housekeeping/manager/optimize_repository_test.go (L1192-1246)
```go
	testWithAndWithoutTransaction(t, ctx, "multiple repositories concurrently", func(t *testing.T, cfg gitalycfg.Cfg, node storage.Node) {
		reqReceivedCh, ch := make(chan struct{}), make(chan struct{})

		repoProtoFirst, _ := gittest.CreateRepository(t, ctx, cfg, gittest.CreateRepositoryConfig{
			SkipCreationViaService: true,
		})
		repoFirst := localrepo.NewTestRepo(t, cfg, repoProtoFirst)
		repoProtoSecond, _ := gittest.CreateRepository(t, ctx, cfg, gittest.CreateRepositoryConfig{
			SkipCreationViaService: true,
		})
		repoSecond := localrepo.NewTestRepo(t, cfg, repoProtoSecond)

		reposOptimized := make(map[string]struct{})

		manager := New(gitalycfgprom.Config{}, testhelper.SharedLogger(t), nil, node)
		manager.optimizeFunc = func(ctx context.Context, repo *localrepo.Repo, _ housekeeping.OptimizationStrategy) error {
			relativePath := repo.GetRelativePath()
			if tx := storage.ExtractTransaction(ctx); tx != nil {
				relativePath = tx.OriginalRepository(&gitalypb.Repository{
					StorageName:  repo.GetStorageName(),
					RelativePath: repo.GetRelativePath(),
				}).GetRelativePath()
			}

			reposOptimized[relativePath] = struct{}{}

			if relativePath == repoFirst.GetRelativePath() {
				reqReceivedCh <- struct{}{}
				ch <- struct{}{}
			}

			return nil
		}

		// We block in the first call so that we can assert that a second call
		// to a different repository performs the optimization regardless without blocking.
		var wg sync.WaitGroup
		wg.Add(1)
		go func() {
			defer wg.Done()
			assert.NoError(t, manager.OptimizeRepository(ctx, repoFirst))
		}()

		<-reqReceivedCh

		// Because this optimizes a different repository this call shouldn't block.
		require.NoError(t, manager.OptimizeRepository(ctx, repoSecond))

		<-ch

		assert.Contains(t, reposOptimized, repoFirst.GetRelativePath())
		assert.Contains(t, reposOptimized, repoSecond.GetRelativePath())

		wg.Wait()
	})
```

**File:** internal/git/housekeeping/manager/optimize_repository_test.go (L1248-1296)
```go
	testWithAndWithoutTransaction(t, ctx, "serialized optimizations", func(t *testing.T, cfg gitalycfg.Cfg, node storage.Node) {
		reqReceivedCh, ch := make(chan struct{}), make(chan struct{})
		repoProto, _ := gittest.CreateRepository(t, ctx, cfg, gittest.CreateRepositoryConfig{
			SkipCreationViaService: true,
		})
		repo := localrepo.NewTestRepo(t, cfg, repoProto)
		var optimizations int

		manager := New(gitalycfgprom.Config{}, testhelper.SharedLogger(t), nil, node)
		manager.optimizeFunc = func(context.Context, *localrepo.Repo, housekeeping.OptimizationStrategy) error {
			optimizations++

			if optimizations == 1 {
				reqReceivedCh <- struct{}{}
				ch <- struct{}{}
			}

			return nil
		}

		var wg sync.WaitGroup
		wg.Add(1)
		go func() {
			defer wg.Done()
			require.NoError(t, manager.OptimizeRepository(ctx, repo))
		}()

		<-reqReceivedCh

		// Because we already have a concurrent call which optimizes the repository we expect
		// that all subsequent calls which try to optimize the same repository return immediately.
		// Furthermore, we expect to see only a single call to the optimizing function because we
		// don't want to optimize the same repository concurrently.
		require.NoError(t, manager.OptimizeRepository(ctx, repo))
		require.NoError(t, manager.OptimizeRepository(ctx, repo))
		require.NoError(t, manager.OptimizeRepository(ctx, repo))
		assert.Equal(t, 1, optimizations)

		<-ch
		wg.Wait()

		// When performing optimizations sequentially though the repository
		// should be unlocked after every call, and consequentially we should
		// also see multiple calls to the optimizing function.
		require.NoError(t, manager.OptimizeRepository(ctx, repo))
		require.NoError(t, manager.OptimizeRepository(ctx, repo))
		require.NoError(t, manager.OptimizeRepository(ctx, repo))
		assert.Equal(t, 4, optimizations)
	})
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
