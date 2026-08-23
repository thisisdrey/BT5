This confirms `DeleteObjectPool` has no membership/dependency tracking at the Gitaly storage layer — repository state (`ObjectPoolStateManager`/`PoolStore`) only exists in a separate metadata scan path [1](#0-0)  used for detecting broken pools after-the-fact, not for gating deletion. The `DeleteObjectPool` RPC handler calls `repoutil.Remove` unconditionally on the pool repository [2](#0-1) , and `repoutil.Remove`/`remove` simply renames and deletes the target directory with no check for dependent alternates [3](#0-2) .

### Title
Unsafe DeleteObjectPool corrupts all linked fork repositories with no dependency accounting - (File: internal/gitaly/service/objectpool/delete.go)

### Summary
`DeleteObjectPool` deletes an object pool repository without verifying whether other repositories are still linked to it via `objects/info/alternates`. This mirrors the reported bug class: one class of dependents (Synth minters with no LP share) has no accounting record, so a legitimate action by another actor (LP withdrawal / pool deletion) destroys the shared resource out from under them. In Gitaly, pool members that rely on the pool's objects through alternates have no first-class, enforced membership record checked at deletion time, so `DeleteObjectPool` can silently corrupt every linked fork.

### Finding Description
An object pool is a shared repository holding objects that multiple fork repositories reference via `objects/info/alternates`, as documented in [4](#0-3) . Linking a repository to a pool (`LinkRepositoryToObjectPool`) sets up this alternates dependency, but nowhere does Gitaly record, at the pool level, which repositories currently depend on it. The `objectpool.ObjectPool` struct built by `FromProto`/`FromRepo` carries no membership list [5](#0-4) .

When `DeleteObjectPool` is invoked, it resolves the pool and calls `repoutil.Remove` directly, with the proto/RPC documentation explicitly stating: "There are no safety checks in place, so if any repository is still using this object pool it will become corrupted" [6](#0-5) . The handler itself performs no lookup of dependents before deleting [2](#0-1) .

Contrast this with `DisconnectGitAlternates`/`Disconnect`, which is the mechanism Gitaly does provide to safely detach a single member: it hard-links all alternate objects into the member first, then removes the alternates file, and runs a connectivity check, rolling back on failure [7](#0-6) . `DeleteObjectPool` performs none of this migration/verification work for any of the pool's members — it just deletes the pool wholesale.

### Impact Explanation
Any caller able to invoke `DeleteObjectPool` for a given pool (the RPC accepts only storage name + relative path, with no verification that the caller "owns" all members) can render every repository still linked to that pool via alternates permanently missing objects — equivalent to the report's scenario where a dependent that holds no accounted "share" of the shared resource loses all of its data when the resource is withdrawn. This is a Denial of Service against every fork/member repository sharing the pool: `git fsck`/reads for commits, trees, and blobs that only exist in the (now-deleted) pool will fail, and there is no automated recovery path once the pool directory has been removed.

### Likelihood Explanation
The scenario requires only ordinary object-pool lifecycle RPCs (`CreateObjectPool`, `LinkRepositoryToObjectPool` on a fork, then `DeleteObjectPool` on the pool) exposed by `ObjectPoolService`, which is reachable through the same RPC surface used for fork creation/deletion flows. No admin/root privilege on the host is required — any client capable of calling this RPC set (e.g., due to a stale/incorrect view of pool membership, a race between fork creation and pool cleanup, or an attacker-controlled `object_pool` field pointing at a pool that still has other members) can trigger the corruption. The explicit code comment acknowledging "no safety checks" (proto/objectpool.proto:44-50) confirms this is a known, reachable gap rather than a purely theoretical one.

### Recommendation
Before deleting an object pool, Gitaly should verify there are no remaining members (e.g., by consulting `ObjectPoolStateManager`/`PoolStore` metadata, or by scanning storage for repositories whose `objects/info/alternates` still point at the pool) and refuse the deletion (or migrate/disconnect all members first, similar to `Disconnect`) unless the caller explicitly confirms no dependents remain.

### Proof of Concept
1. `CreateObjectPool` from repository A, producing pool P.
2. `LinkRepositoryToObjectPool` to link repository B (a fork of A) to P; B's `objects/info/alternates` now points at P, and B's own pack files may have been repacked to remove objects now solely held by P.
3. Call `DeleteObjectPool` on P.
4. `repoutil.Remove` deletes P's directory with no check on B's dependency [2](#0-1) .
5. Any subsequent read of B's history that resolves to objects previously deduplicated into P now fails (`git fsck`, `git cat-file`, clone/fetch of B all error on missing objects), with no restore mechanism, analogous to the Synth holder being unable to withdraw after the LP removes all liquidity.

### Citations

**File:** internal/gitaly/service/internalgitaly/scan_pool_metadata.go (L36-60)
```go
func processPoolMemberFunc(
	ctx context.Context,
	storagePath, storageName string,
	poolStore relational.PoolStore,
	logger log.Logger,
	stream gitalypb.InternalGitaly_ScanPoolMetadataServer,
) func(relPath string, _ fs.FileInfo) error {
	invalidPools := make(map[string]bool)

	return func(relPath string, fi fs.FileInfo) error {
		repoPath := filepath.Join(storagePath, relPath)

		altInfo, err := stats.AlternatesInfoForRepository(repoPath)
		if err != nil {
			return fmt.Errorf("read alternates for %q: %w", relPath, err)
		}

		if !altInfo.Exists || len(altInfo.ObjectDirectories) == 0 {
			return nil
		}

		absPoolPaths := altInfo.AbsoluteObjectDirectories()
		if len(absPoolPaths) == 0 {
			return nil
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

**File:** internal/gitaly/repoutil/remove.go (L21-147)
```go
// Remove will remove a repository in a race-free way with proper transactional semantics.
func Remove(
	ctx context.Context,
	logger log.Logger,
	locator storage.Locator,
	txManager transaction.Manager,
	repoCounter *counter.RepositoryCounter,
	repository storage.Repository,
) error {
	if err := remove(ctx, logger, locator, txManager, repository, os.RemoveAll); err != nil {
		return err
	}

	repoCounter.Decrement(repository)

	return nil
}

func remove(
	ctx context.Context,
	logger log.Logger,
	locator storage.Locator,
	txManager transaction.Manager,
	repository storage.Repository,
	removeAll func(string) error,
) error {
	path, err := locator.GetRepoPath(ctx, repository, storage.WithRepositoryVerificationSkipped())
	if err != nil {
		return structerr.NewInternal("%w", err)
	}

	if tx := storage.ExtractTransaction(ctx); tx != nil {
		tx.DeleteRepository()

		originalRelativePath, err := filepath.Rel(tx.FS().Root(), path)
		if err != nil {
			return fmt.Errorf("original relative path: %w", err)
		}

		if err := storage.RecordDirectoryRemoval(tx.FS(), tx.FS().Root(), originalRelativePath); err != nil {
			return fmt.Errorf("record directory removal: %w", err)
		}

		if err := tx.KV().Delete(storage.RepositoryKey(originalRelativePath)); err != nil {
			return fmt.Errorf("delete repository key: %w", err)
		}
	}

	tempDir, err := locator.TempDir(repository.GetStorageName())
	if err != nil {
		return structerr.NewInternal("temporary directory: %w", err)
	}

	if err := os.MkdirAll(tempDir, mode.Directory); err != nil {
		return structerr.NewInternal("%w", err)
	}

	// Check whether the repository exists. If not, then there is nothing we can
	// remove. Historically, we didn't return an error in this case, which was just
	// plain bad RPC design: callers should be able to act on this, and if they don't
	// care they may still just return `NotFound` errors.
	if _, err := os.Stat(path); err != nil {
		if os.IsNotExist(err) {
			return structerr.NewNotFound("repository does not exist")
		}

		return structerr.NewInternal("statting repository: %w", err)
	}

	if err := voteOnAction(ctx, txManager, repository, voting.Preparing); err != nil {
		return structerr.NewInternal("vote on rename: %w", err)
	}
	// Lock the repository such that it cannot be created or removed by any concurrent
	// RPC call.
	unlock, err := Lock(ctx, logger, locator, repository)
	if err != nil {
		if errors.Is(err, safe.ErrFileAlreadyLocked) {
			return structerr.NewFailedPrecondition("repository is already locked")
		}
		return structerr.NewInternal("locking repository for removal: %w", err)
	}
	defer unlock()

	// Recheck whether the repository still exists after we have taken the lock. It
	// could be a concurrent RPC call removed the repository while we have not yet been
	// holding the lock.
	if _, err := os.Stat(path); err != nil {
		if os.IsNotExist(err) {
			return structerr.NewNotFound("repository was concurrently removed")
		}
		return structerr.NewInternal("re-statting repository: %w", err)
	}

	if err := voteOnAction(ctx, txManager, repository, voting.Prepared); err != nil {
		return structerr.NewInternal("vote on rename: %w", err)
	}

	destDir, err := os.MkdirTemp(tempDir, filepath.Base(path)+"+removed-*")
	if err != nil {
		return fmt.Errorf("mkdir temp: %w", err)
	}

	defer func() {
		if err := removeAll(destDir); err != nil {
			logger.WithError(err).ErrorContext(ctx, "failed removing repository from temporary directory")
		}
	}()

	// We move the repository into our temporary directory first before we start to
	// delete it. This is done such that we don't leave behind a partially-removed and
	// thus likely corrupt repository.
	if err := os.Rename(path, filepath.Join(destDir, "repo")); err != nil {
		return structerr.NewInternal("staging repository for removal: %w", err)
	}

	if storage.NeedsSync(ctx) {
		if err := safe.NewSyncer().SyncParent(ctx, path); err != nil {
			return fmt.Errorf("sync removal: %w", err)
		}
	}

	if err := voteOnAction(ctx, txManager, repository, voting.Committed); err != nil {
		return structerr.NewInternal("vote on finalizing: %w", err)
	}

	return nil
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

**File:** internal/git/objectpool/pool.go (L36-91)
```go
type ObjectPool struct {
	*localrepo.Repo

	logger              log.Logger
	locator             storage.Locator
	gitCmdFactory       gitcmd.CommandFactory
	txManager           transaction.Manager
	housekeepingManager housekeepingmgr.Manager
}

// FromProto returns an object pool object from its Protobuf representation. This function verifies
// that the object pool exists and is a valid pool repository.
func FromProto(
	ctx context.Context,
	logger log.Logger,
	locator storage.Locator,
	gitCmdFactory gitcmd.CommandFactory,
	catfileCache catfile.Cache,
	txManager transaction.Manager,
	housekeepingManager housekeepingmgr.Manager,
	proto *gitalypb.ObjectPool,
) (*ObjectPool, error) {
	poolPath, err := locator.GetRepoPath(ctx, proto.GetRepository(), storage.WithRepositoryVerificationSkipped())
	if err != nil {
		return nil, err
	}

	if !storage.IsPoolRepository(proto.GetRepository()) {
		// When creating repositories in the ObjectPool service we will first create the
		// repository in a temporary directory. So we need to check whether the path we see
		// here is in such a temporary directory and let it pass.
		tempDir, err := locator.TempDir(proto.GetRepository().GetStorageName())
		if err != nil {
			return nil, fmt.Errorf("getting temporary storage directory: %w", err)
		}

		if !strings.HasPrefix(poolPath, tempDir) {
			return nil, ErrInvalidPoolDir
		}
	}

	pool := &ObjectPool{
		Repo:                localrepo.New(logger, locator, gitCmdFactory, catfileCache, proto.GetRepository()),
		logger:              logger,
		locator:             locator,
		gitCmdFactory:       gitCmdFactory,
		txManager:           txManager,
		housekeepingManager: housekeepingManager,
	}

	if !pool.IsValid(ctx) {
		return nil, ErrInvalidPoolRepository
	}

	return pool, nil
}
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

**File:** internal/git/objectpool/disconnect.go (L24-35)
```go
// Disconnect disconnects the specified repository from its object pool. If the repository does not
// utilize an alternate object database, no error is returned. For repositories that depend on
// alternate objects, the following steps are performed:
//   - Alternate objects are hard-linked to the main repository.
//   - The repository's Git alternates file is backed up and object pool disconnected.
//   - A connectivity check is performed to ensure the repository is complete. If this check fails,
//     the repository is reconnected to the object pool via the backup and an error returned.
//
// This operation carries some risk. If the repository is in a broken state, it will not be restored
// until after the connectivity check completes. If Gitaly crashes before the backup is restored,
// the repository may be in a broken state until an administrator intervenes and restores the backed
// up copy of objects/info/alternates.
```
