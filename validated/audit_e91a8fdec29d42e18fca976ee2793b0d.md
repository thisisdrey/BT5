### Title
`DeleteObjectPool` performs no membership/ownership check before destroying a shared object pool - ([File: internal/gitaly/service/objectpool/delete.go])

### Summary
The Palmera finding shows `removeSafe()` lacking the access-control gate ("must be called by root safe") that the protocol design requires, so any registered-but-unprivileged caller can destroy a shared resource and corrupt dependent state for other parties. The analogous condition exists in Gitaly's `ObjectPoolService.DeleteObjectPool` RPC: any client that can reach the RPC and knows (or can derive) a pool repository's `storage_name`/`relative_path` can delete it outright, with no verification that the pool is unused. The docstring itself admits this is by design ("There are no safety checks in place, so if any repository is still using this object pool it will become corrupted") [1](#0-0) , but nothing in the RPC handler or transport enforces that only the entity responsible for the pool's full lifecycle (i.e., the "root" owner of all its members) can invoke this destructive, cross-repository operation.

### Finding Description
`DeleteObjectPool` resolves the object pool purely from caller-supplied repository coordinates and immediately removes it from disk: [2](#0-1) 

`poolForRequest` only validates that the path parses as a syntactically valid pool directory and that the pool is a valid git repository — it performs no check regarding which repositories are still linked (alternates) to that pool, nor any authorization concept tying "who may delete this pool" to "who owns/administers all of its members": [3](#0-2) 

The `LinkRepositoryToObjectPool` / `FetchIntoObjectPool` / `DisconnectGitAlternates` RPCs establish the pool-membership lifecycle, and the design explicitly delegates the safety invariant ("the pool has no members before deletion") to the caller rather than to Gitaly: [4](#0-3) 

Because a pool repository's `relative_path` is derivable/known (e.g. `@pools/<sha>` hashed paths or Praefect-derived `@cluster/pools/...` paths) and is not itself a secret, and because Gitaly's authorization model is per-storage-path rather than per logical "owner of the fork network," any client authorized to call ObjectPoolService RPCs against a given storage (the equivalent of "SafeRegistered" in the Palmera analogy — an already-onboarded, unprivileged caller, not the "root" party that should exclusively control the pool's lifecycle) can issue `DeleteObjectPool` for a pool it does not exclusively own. This mirrors the Palmera bug class: an operation that should require a stronger, singular authority ("root safe" / "sole responsible pool owner") is instead reachable by any caller possessing valid-but-ordinary access, because the access-control/precondition check that the documentation implies is required was never implemented in code — it is only documented as a caller responsibility.

### Impact Explanation
Deleting an in-use object pool corrupts every repository that still holds an `objects/info/alternates` link to it, since those repositories are missing objects the pool held (this is directly acknowledged in `doc/object_pools.md` and the `DeleteObjectPool` RPC doc comment) [5](#0-4) [1](#0-0) . This is a cross-repository availability/integrity impact: one caller's action against a resource it does not exclusively administer can silently break other, unrelated repositories that still depend on the pool (e.g. other forks in the same pool network), analogous to how `removeSafe()` letting a non-root caller reassign/detach children corrupts the intended hierarchy for parties who did not consent to the operation.

### Likelihood Explanation
Reaching this requires only a valid gRPC call to `ObjectPoolService.DeleteObjectPool` with a known pool repository's storage/relative path — no special privilege beyond ordinary authenticated Gitaly access is enforced by the handler itself. Because pool paths are deterministic/derivable (hashed from repository ID or fork source), and no membership check gates the delete, likelihood of accidental or malicious misuse is realistic in any environment where the calling layer (e.g., Rails/GitLab) does not itself perfectly synchronize "no members left" before calling this RPC, or where a caller with access to the RPC surface (but not true ownership of the entire pool's member set) can invoke it directly.

### Recommendation
Before removing the pool, `DeleteObjectPool` should verify there are no remaining members (e.g., by checking that no repository under the same storage still holds an alternates link to the pool, or by requiring an authoritative signal from the exclusive fork-network owner) and reject the RPC (e.g., `FailedPrecondition`) if members are still linked, similar to how `LinkRepositoryToObjectPool`/`DisconnectGitAlternates` already track pool linkage state. This closes the gap between the documented intended access-control contract ("caller must ensure no members left") and what the code actually enforces.

### Proof of Concept
1. Create repository `A`, create an object pool `P` from it via `CreateObjectPool`, and link `A` to `P` via `LinkRepositoryToObjectPool` (per the flow in `internal/gitaly/service/objectpool/link_test.go`, e.g. `TestCompleteForkCreationFlow`) [6](#0-5) .
2. As any caller able to reach the ObjectPoolService (not necessarily the "owner" managing the fork network), invoke `DeleteObjectPool` with `P`'s repository coordinates:
```go
client.DeleteObjectPool(ctx, &gitalypb.DeleteObjectPoolRequest{
    ObjectPool: &gitalypb.ObjectPool{Repository: poolProto},
})
```
This succeeds unconditionally via `repoutil.Remove` [2](#0-1) , with no check that `A` (or any other member) is still linked.
3. Subsequent operations against `A` that rely on deduplicated objects in `P` (e.g. reading commits only present in the pool) will fail/corrupt, since the alternates target no longer exists — the exact corruption scenario the RPC's own documentation and `doc/object_pools.md` warn about [5](#0-4) .

**Note:** I was not able to fully verify whether an upper layer (e.g., GitLab Rails or Praefect) enforces additional authorization/ownership checks before calling `DeleteObjectPool` in production deployments — the Gitaly-level handler itself, as shown above, contains no such check. If such an external gate exists and is the sole enforcement point, likelihood should be reassessed accordingly.

### Citations

**File:** proto/objectpool.proto (L10-30)
```text
// ObjectPoolService is a service that manages the lifetime of object pools.
//
// An object pool is a separate repository that can be linked to from multiple object pool members
// in order to deduplicate common objects between them. This is mostly used in the context of
// repository forks.
//
// The typical lifetime of an object pool is as follows:
//
// 1. An object pool is created via CreateObjectPool from its primary pool member. Typically this
//    would be the repository that gets forked.
// 2. One or more repositories are linked to the object pool via LinkRepositoryToObjectPool. Each
//    object pool member linked to the repository will have its objects deduplicated when its
//    objects get repacked the next time.
// 3. The object pool is regularly updated via FetchIntoObjectPool. This is typically only done from
//    the primary object pool member.
// 4. Repositories may leave the object pool via DisconnectGitAlternates. There is not much of a
//    reason to do this for any repositories except for the primary object pool member in case it
//    for example becomes private.
// 5. When the object pool does not have any members anymore it gets deleted via DeleteObjectPool.
//    It is the responsibility of the caller to ensure that it really has no members left, else
//    any existing member will become corrupt.
```

**File:** proto/objectpool.proto (L44-50)
```text
  // DeleteObjectPool deletes the object pool. There are no safety checks in place, so if any
  // repository is still using this object pool it will become corrupted.
  rpc DeleteObjectPool(DeleteObjectPoolRequest) returns (DeleteObjectPoolResponse) {
    option (op_type) = {
      op: MUTATOR
    };
  }
```

**File:** internal/gitaly/service/objectpool/delete.go (L15-29)
```go
func (s *server) DeleteObjectPool(ctx context.Context, in *gitalypb.DeleteObjectPoolRequest) (*gitalypb.DeleteObjectPoolResponse, error) {
	pool, err := s.poolForRequest(ctx, in)
	if err != nil {
		if errors.Is(err, objectpool.ErrInvalidPoolRepository) {
			// TODO: we really should return an error in case we're trying to delete an
			// object pool that does not exist.
			return &gitalypb.DeleteObjectPoolResponse{}, nil
		}

		return nil, err
	}

	if err := repoutil.Remove(ctx, s.logger, s.locator, nil, s.repositoryCounter, pool); err != nil {
		return nil, fmt.Errorf("remove: %w", err)
	}
```

**File:** internal/gitaly/service/objectpool/util.go (L35-50)
```go
func (s *server) poolForRequest(ctx context.Context, req PoolRequest) (*objectpool.ObjectPool, error) {
	pool, err := objectpool.FromProto(ctx, s.logger, s.locator, s.gitCmdFactory, s.catfileCache, s.txManager, s.housekeepingManager, req.GetObjectPool())
	if err != nil {
		if errors.Is(err, objectpool.ErrInvalidPoolDir) {
			return nil, errInvalidPoolDir
		}

		if errors.Is(err, objectpool.ErrInvalidPoolRepository) {
			return nil, structerr.NewFailedPrecondition("%w", err)
		}

		return nil, structerr.NewInternal("%w", err)
	}

	return pool, nil
}
```

**File:** doc/object_pools.md (L35-43)
```markdown
Removing a member from an object pool is slightly more involved, as members of
an object pool members will miss objects which are only part of the object pool.
It is thus not as simple as removing `objects/info/alternates`, as that would
leave behind a corrupt repository. Instead, Gitaly hard-links all objects which
are part of the object pool into the dissociating member first and removes the
alternate afterwards. In order to check whether the operation succeeded, Gitaly
now runs `git-fsck(1)` to check for missing objects. If there are none, the
dissociation has succeeded. Otherwise, it will fail and re-add the alternates
file.
```

**File:** internal/gitaly/service/objectpool/link_test.go (L21-76)
```go
func TestCompleteForkCreationFlow(t *testing.T) {
	t.Parallel()

	ctx := testhelper.Context(t)

	cfg, sourceRepository, _, _, objectPoolClient := setup(t, ctx, testserver.WithDisablePraefect())

	repositoryClient := gitalypb.NewRepositoryServiceClient(
		objectPoolClient.(clientWithConn).conn,
	)

	forkRepository := &gitalypb.Repository{
		StorageName:  sourceRepository.GetStorageName(),
		RelativePath: gittest.NewRepositoryName(t),
	}

	// Inject the Gitaly's address information in the context. CreateFork uses this to
	// fetch from the source repository.
	ctx = testhelper.MergeOutgoingMetadata(ctx, testcfg.GitalyServersMetadataFromCfg(t, cfg))
	// Build GitalySSH as CreateFork uses to perform the fetch.
	testcfg.BuildGitalySSH(t, cfg)

	// Rails sends a RepositoryExists request before creating the fork as well.
	repositoryExistsResponse, err := repositoryClient.RepositoryExists(ctx, &gitalypb.RepositoryExistsRequest{
		Repository: forkRepository,
	})
	require.NoError(t, err)
	testhelper.ProtoEqual(t, &gitalypb.RepositoryExistsResponse{
		Exists: false,
	}, repositoryExistsResponse)

	createForkResponse, err := repositoryClient.CreateFork(ctx, &gitalypb.CreateForkRequest{
		Repository:       forkRepository,
		SourceRepository: sourceRepository,
	})
	require.NoError(t, err)
	testhelper.ProtoEqual(t, &gitalypb.CreateForkResponse{}, createForkResponse)

	// Create an object pool from the source repository.
	objectPool, _, _ := createObjectPool(t, ctx, cfg, sourceRepository)

	// Link the source repository itself to the object pool.
	linkSourceToObjectPoolResponse, err := objectPoolClient.LinkRepositoryToObjectPool(ctx, &gitalypb.LinkRepositoryToObjectPoolRequest{
		ObjectPool: objectPool,
		Repository: sourceRepository,
	})
	require.NoError(t, err)
	testhelper.ProtoEqual(t, &gitalypb.LinkRepositoryToObjectPoolResponse{}, linkSourceToObjectPoolResponse)

	// Link the fork to the object pool.
	linkForkToObjectPoolResponse, err := objectPoolClient.LinkRepositoryToObjectPool(ctx, &gitalypb.LinkRepositoryToObjectPoolRequest{
		ObjectPool: objectPool,
		Repository: forkRepository,
	})
	require.NoError(t, err)
	testhelper.ProtoEqual(t, &gitalypb.LinkRepositoryToObjectPoolResponse{}, linkForkToObjectPoolResponse)
```
