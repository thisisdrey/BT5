### Title
DeleteObjectPool RPC destroys a pool with no check that member repositories are still linked, corrupting all dependent repositories - (File: internal/gitaly/service/objectpool/delete.go)

### Summary
`DeleteObjectPool` removes an object pool repository unconditionally, without verifying that no repository is still linked to it via its Git `alternates` file. This mirrors the reported bug class of "insufficient connection check": one side of a bidirectional dependency (pool ↔ member) is deleted/altered without confirming the other side's state, leaving dependents in a broken condition that only manifests when they are next accessed.

### Finding Description
Object pools implement object deduplication for forked repositories by having member repositories point their `objects/info/alternates` file at the pool's object directory [1](#0-0) . The relationship is intentionally one-directional: each member repository knows which pool it is linked to (`Link`/`linkedToRepository` check the member's alternates file against the expected pool path) [2](#0-1) , but the pool itself keeps no record of which repositories depend on it.

`DeleteObjectPool` exploits this asymmetry: it looks up the pool from the request and calls `repoutil.Remove` to delete it, with no step that enumerates or checks members that still reference it through `alternates`: [3](#0-2) 

The proto documentation for this RPC explicitly acknowledges the missing mutual-connection check: "There are no safety checks in place, so if any repository is still using this object pool it will become corrupted" [4](#0-3) .

Contrast this with `DisconnectGitAlternates`, which performs the correct mutual verification (re-links objects into the member and runs `git-fsck` before it lets the member drop the alternate) [5](#0-4) . `DeleteObjectPool` provides no equivalent counterpart-side verification: it never checks whether any repository's `objects/info/alternates` still points at the pool being removed.

### Impact Explanation
Any repository still linked to the deleted pool becomes permanently corrupted: subsequent Git operations (fetch, clone, `cat-file`, hooks needing quarantined/pool objects) will fail because objects that were deduplicated into the pool are now unreachable. This is a stronger, permanent analog of the reported "frozen funds" bug — rather than a recoverable timeout, member repositories suffer irrecoverable object loss and require a fsck/reconciliation or repository re-creation to be usable again, i.e., a data-integrity/DoS impact on legitimate, uninvolved repositories that trusted the pool link.

### Likelihood Explanation
The RPC is a normal `MUTATOR` operation in `ObjectPoolService` [6](#0-5) , reachable by any caller (e.g., Rails/Workhorse-driven pool lifecycle management, or any client with access to the ObjectPoolService) that can supply a crafted `DeleteObjectPoolRequest` referencing a pool that still has active members — no special/privileged git-hosting access to the member repositories is required, only knowledge of the pool's identity, which is itself returned by `GetObjectPool` for any repository that queries it [7](#0-6) .

### Recommendation
Before removing the pool repository in `DeleteObjectPool`, enumerate repositories in the storage (or track pool membership explicitly) and verify none of them still reference the pool via `objects/info/alternates`; refuse deletion (or force a disconnect/fsck-verified migration per member, as `DisconnectGitAlternates` already does) if any member is still linked.

### Proof of Concept
1. Create repository `A`; call `CreateObjectPool` with origin `A` to create pool `P`.
2. Call `LinkRepositoryToObjectPool(repository=A, objectPool=P)` — `A`'s `objects/info/alternates` now points at `P`'s object directory, and objects unique to `A` may be deduplicated into `P` on repack.
3. Call `DeleteObjectPool(objectPool=P)` — the handler removes `P` immediately with no check of `A`'s alternates state [3](#0-2) .
4. Any subsequent operation on `A` that needs objects that lived only in `P` (e.g. `git fsck`, `cat-file`, clone) fails with missing-object errors, since `A` never migrated the pool's objects back and the pool no longer exists.

### Citations

**File:** doc/object_pools.md (L10-12)
```markdown
The sharing of objects for a given repository and its object pool is done via
alternate object directories which Gitaly sets up when linking a repository to
an object pool by writing the `objects/info/alternates` file.
```

**File:** internal/git/objectpool/link.go (L168-204)
```go
// linkedToRepository tests if a repository is linked to an object pool
func linkedToRepository(ctx context.Context, pool, repo *localrepo.Repo) (bool, error) {
	poolPath, err := pool.Path(ctx)
	if err != nil {
		return false, fmt.Errorf("getting object pool path: %w", err)
	}

	repoPath, err := repo.Path(ctx)
	if err != nil {
		return false, fmt.Errorf("getting repo path: %w", err)
	}

	altInfo, err := stats.AlternatesInfoForRepository(repoPath)
	if err != nil {
		return false, fmt.Errorf("getting alternates info: %w", err)
	}

	if !altInfo.Exists || len(altInfo.ObjectDirectories) == 0 {
		return false, nil
	}

	relPath := altInfo.ObjectDirectories[0]
	expectedRelPath, err := getRelativeObjectPath(ctx, pool, repo)
	if err != nil {
		return false, err
	}

	if relPath == expectedRelPath {
		return true, nil
	}

	if filepath.Clean(relPath) != filepath.Join(poolPath, "objects") {
		return false, fmt.Errorf("unexpected alternates content: %q", relPath)
	}

	return false, nil
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

**File:** internal/gitaly/service/objectpool/get.go (L13-42)
```go
func (s *server) GetObjectPool(ctx context.Context, in *gitalypb.GetObjectPoolRequest) (*gitalypb.GetObjectPoolResponse, error) {
	repository := in.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repository); err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}

	repo := s.localRepoFactory.Build(repository)
	objectPool, err := objectpool.FromRepo(ctx, s.logger, s.locator, s.gitCmdFactory, s.catfileCache, s.txManager, s.housekeepingManager, repo)
	if err != nil && !errors.Is(err, objectpool.ErrAlternateObjectDirNotExist) {
		s.logger.
			WithError(err).
			WithField("storage", repository.GetStorageName()).
			WithField("relative_path", repository.GetRelativePath()).
			WarnContext(ctx, "alternates file does not point to valid git repository")
	}

	if objectPool == nil {
		return &gitalypb.GetObjectPoolResponse{}, nil
	}

	objectPoolProto := objectPool.ToProto()
	if tx := storage.ExtractTransaction(ctx); tx != nil {
		// The object pool's relative path is pointing to the transaction's snapshot. Return
		// the original relative path in the response.
		objectPoolProto.Repository = tx.OriginalRepository(objectPoolProto.GetRepository())
	}

	return &gitalypb.GetObjectPoolResponse{
		ObjectPool: objectPoolProto,
	}, nil
```
