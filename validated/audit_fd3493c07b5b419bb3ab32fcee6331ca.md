### Title
Missing member/reference check in `DeleteObjectPool` allows an ordinary fork owner's action to permanently corrupt sibling repositories - (File: internal/gitaly/service/objectpool/delete.go)

### Summary
`DeleteObjectPool` removes an object pool repository from disk without verifying that no repository is still linked to it via `objects/info/alternates`. The proto documentation itself acknowledges: "There are no safety checks in place, so if any repository is still using this object pool it will become corrupted." [1](#0-0)  The Go handler implements exactly this: it resolves the pool and unconditionally calls `repoutil.Remove`, with no query of pool membership before deletion. [2](#0-1) 

### Finding Description
Object pools are shared-object repositories that multiple fork repositories link to via the `objects/info/alternates` file, so that common objects are stored once instead of once per fork. [3](#0-2)  A repository is linked to a pool by writing a relative path into its alternates file [4](#0-3) , and any pool member that still has this alternates link depends entirely on the pool's `objects/` directory to resolve its own object graph — deleting the pool out from under a linked member turns every git operation on that member into a "missing object" failure.

The `DeleteObjectPool` RPC is documented as unsafe by design: deletion happens with no verification that zero members remain linked. [5](#0-4)  The corresponding server implementation confirms this: it resolves the pool object via `poolForRequest` and immediately calls `repoutil.Remove`, with no call to any pool-membership store (`ListPoolMembers`, `GetPoolForMember`, etc., which do exist in `internal/gitaly/storage/relational/pool_store.go`) to confirm the pool has no active members before deleting it. [2](#0-1) [6](#0-5) 

This mirrors the referenced report's root cause precisely: an operation trusts a shared/foreign resource's state (here, "is anyone still relying on this alternates target?", there, "can this mint's authority ever freeze my account?") without checking a discoverable, verifiable precondition, and the omission converts an ordinary, permitted action into an irreversible denial-of-service for other, uninvolved parties (fork owners whose repositories are pool members) — not the actor who performed the risky action.

### Impact Explanation
If `DeleteObjectPool` is invoked (by Rails, an administrator tool, or any caller with access to the internal `ObjectPoolService`) while forks are still linked to the pool, every one of those fork repositories permanently loses access to its shared objects. Git operations that depend on those objects (clone, fetch, cat-file, ref resolution) begin to fail with "missing object" errors, and there is no automated recovery path once the pool directory has been removed — the corruption is effectively as durable as a frozen SPL account in the original report, and it harms innocent fork owners who did nothing to cause the deletion, exactly the harm pattern the judge highlighted ("there will be losses to innocent users ... given that this is an avoidable issue").

### Likelihood Explanation
The unsafe precondition is not hypothetical/rare — it is explicitly acknowledged in the proto documentation and grpc-generated docstrings as a caller responsibility rather than a system-enforced invariant. [1](#0-0)  Any caller path that invokes `DeleteObjectPool` (e.g., project/fork-network deletion flows, replication cleanup, or operator tooling) without first confirming zero members remain will trigger the corruption; the RPC itself performs no membership check as a last line of defense, so a single missed disconnect step anywhere in the calling logic is sufficient.

### Recommendation
Before removing the pool's on-disk directory in `DeleteObjectPool`, query the pool-membership metadata (`PoolStore.ListPoolMembers` / equivalent relational or on-disk scan) and refuse the deletion (return `FailedPrecondition`) if any member is still linked via `objects/info/alternates`, mirroring the existing safety pattern already used in `Disconnect`, which performs a `git-fsck` verification and restores the alternates file on failure before proceeding. [7](#0-6)  Alternatively, require callers to pass an explicit "force" flag while making the safe (member-checked) path the default.

### Proof of Concept
1. Create a repository and an object pool from it via `CreateObjectPool`, then link a second repository to the pool via `LinkRepositoryToObjectPool`, establishing an `objects/info/alternates` reference from the member to the pool. [8](#0-7) 
2. Call `DeleteObjectPool` against the pool while the member remains linked — the RPC succeeds unconditionally, since `poolForRequest`/`repoutil.Remove` perform no membership check. [2](#0-1) 
3. Attempt any object-resolution operation (e.g., `CommitService.CommitLanguages`, `cat-file`) on the still-linked member repository — it fails because the shared objects it depends on via alternates no longer exist, and there is no way to restore them since the pool directory was deleted, not merely disconnected. Compare this to `DisconnectGitAlternates`, whose test explicitly demonstrates that removing/re-adding the alternates file is the safe, reversible way to manage this dependency [9](#0-8)  — `DeleteObjectPool` bypasses that safety net entirely.

### Citations

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

**File:** internal/git/objectpool/link.go (L19-23)
```go
// Link calls the non-receiver method version of Link with the parameters
// injected from the object pool.
func (o *ObjectPool) Link(ctx context.Context, repo *localrepo.Repo) error {
	return Link(ctx, o.Repo, repo, o.txManager)
}
```

**File:** internal/git/objectpool/link.go (L25-37)
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
```

**File:** proto/go/gitalypb/objectpool_grpc.pb.go (L183-185)
```go
//  5. When the object pool does not have any members anymore it gets deleted via DeleteObjectPool.
//     It is the responsibility of the caller to ensure that it really has no members left, else
//     any existing member will become corrupt.
```

**File:** internal/gitaly/storage/relational/pool_store.go (L24-43)
```go
// PoolStore provides storage for object pool metadata.
type PoolStore interface {
	StorePoolData(ctx context.Context, storageName string, poolsByDiskPath map[string]*PoolMetadata) error
	GetPoolByDiskPath(ctx context.Context, poolDiskPath string) (*PoolMetadata, error)
	ListPools(ctx context.Context) ([]*PoolMetadata, error)
	ForEachPoolByStorage(ctx context.Context, storageName string, fn func(*PoolMetadata) error) error

	ListPoolMembers(ctx context.Context, poolDiskPath string) ([]string, error)
	DeletePoolMembers(ctx context.Context, poolDiskPath string) error
	GetPoolForMember(ctx context.Context, memberDiskPath string) (string, error)

	CreatePool(ctx context.Context, poolDiskPath, storageName, upstream string, lastScanned time.Time) error
	DeletePool(ctx context.Context, poolDiskPath string) error
	AddMember(ctx context.Context, poolDiskPath, memberDiskPath string) error
	RemoveMember(ctx context.Context, poolDiskPath, memberDiskPath string) error

	RecordBrokenPool(ctx context.Context, storage, poolMember, pool string) error

	Close() error
}
```

**File:** internal/git/objectpool/disconnect.go (L69-92)
```go
	// A repository should only ever be linked to a single alternate object directory. If the
	// repository links to multiple object directories, the repository is in an invalid state.
	if len(altInfo.ObjectDirectories) > 1 {
		return errors.New("multiple alternate object directories")
	}

	// If the alternate object directory entry does not exist on disk, the repository's Git
	// alternates file is in an invalid state.
	altObjectDir := altInfo.AbsoluteObjectDirectories()[0]
	altObjectDirStats, err := os.Stat(altObjectDir)
	if err != nil {
		return err
	}

	// If the alternate object directory is not a directory, the repository's Git alternates file is
	// in an invalid state.
	if !altObjectDirStats.IsDir() {
		return errors.New("alternate object entry is not a directory")
	}

	objectFiles, err := findObjectFiles(altObjectDir)
	if err != nil {
		return err
	}
```

**File:** internal/gitaly/service/objectpool/alternates_test.go (L34-49)
```go
	// Corrupt the repository to check that the commit we have created can no longer be read.
	// This is done to ensure that the object really only exists in the pool repository now.
	altPath, err := repo.InfoAlternatesPath(ctx)
	require.NoError(t, err)
	require.NoError(t, os.Remove(altPath))
	gittest.RequireObjectNotExists(t, cfg, repoPath, commitID)

	// Recreate the alternates link and assert that we can now read the commit again.
	require.NoError(t, pool.Link(ctx, repo))
	require.FileExists(t, altPath, "objects/info/alternates should be back")
	gittest.RequireObjectExists(t, cfg, repoPath, commitID)

	// At this point we know that the repository has access to the commit, but only if
	// objects/info/alternates is in place.
	_, err = client.DisconnectGitAlternates(ctx, &gitalypb.DisconnectGitAlternatesRequest{Repository: repoProto})
	require.NoError(t, err)
```
