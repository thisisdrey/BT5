Given the report's bug class — an operation that finalizes/cleans up a shared container (closing the withdrawal window) without first verifying or completing the transfer of dependent state out of it — the closest concrete Gitaly analog is `DeleteObjectPool`, which destroys an object pool repository without verifying that all pool members have already been disconnected (i.e., had their shared objects "transferred out" via `DisconnectGitAlternates`) first.

### Title
Object pool deletion does not verify all members are disconnected before destroying the shared pool - ([File: internal/gitaly/service/objectpool/delete.go])

### Summary
`DeleteObjectPool` removes the object pool repository from disk unconditionally, without checking whether any repository is still linked to it via `objects/info/alternates`. Any member repository that has not gone through `DisconnectGitAlternates` (which hard-links shared objects back into the member *before* the pool can safely go away) will be left pointing at a nonexistent alternate object directory, silently losing access to all objects it never migrated out of the pool.

### Finding Description
The lifecycle of an object pool documented in `proto/objectpool.proto` requires that repositories "leave the object pool via `DisconnectGitAlternates`" — which links pool objects into the member and only then removes the alternates entry — before the pool is deleted with `DeleteObjectPool`. [1](#0-0)  The `DisconnectGitAlternates` implementation performs exactly this "transfer before close" sequence: it hard-links every object from the pool's alternate directory into the member repository, and only afterward removes the alternates file, guarded by a `git rev-list` connectivity check. [2](#0-1) [3](#0-2) 

However, `DeleteObjectPool`'s handler performs no such check — it looks up the pool and immediately calls `repoutil.Remove` on it, with no verification that zero repositories still reference it as an alternate: [4](#0-3) 

This mirrors the reported bug pattern precisely: the "restake" (here, `DeleteObjectPool`) finalizes/removes the shared container without first ensuring the "base tokens" (here, the shared objects still depended upon via alternates) have been transferred out of it into every dependent (here, pool member repositories). The proto comment even flags this as a known risk ("There are no safety checks in place, so if any repository is still using this object pool it will become corrupted" / "It is the responsibility of the caller to ensure that it really has no members left"), but this responsibility is not enforced by Gitaly itself. [5](#0-4) 

### Impact Explanation
If `DeleteObjectPool` is invoked (e.g. by a client/Rails race condition, a buggy caller, or a malicious/compromised caller with only object-pool RPC access and no special privilege over the individual member repositories) while any fork/member repository still has an `objects/info/alternates` entry pointing at that pool, every such member instantly loses access to all objects it never migrated locally. Subsequent Git operations against these members (clone, fetch, read, `git-fsck`) will fail with missing-object errors, effectively corrupting/denial-of-servicing repositories that the caller of `DeleteObjectPool` may not even own or have write access to. This is a cross-repository blast-radius issue: one gRPC call against the pool repository can silently corrupt unrelated fork repositories.

### Likelihood Explanation
`DeleteObjectPool` is a standard mutator RPC reachable by anyone with access to call the `ObjectPoolService` for a given pool (e.g. via Rails backend logic, or directly if access controls are looser than assumed). No special condition is required beyond the caller not having first disconnected every member — which can happen due to caller bugs, race conditions between disconnect and delete calls, or intentional misuse, since Gitaly performs no server-side safety check.

### Recommendation
Before removing the object pool's on-disk data in `DeleteObjectPool`, verify (e.g., via `RepositoryStore`/relational pool-member tracking already used elsewhere, such as `ListPoolMembers`) that no repository is still linked to the pool, and refuse the deletion (or automatically disconnect remaining members) if members exist. At minimum, log/alert and require an explicit force flag, rather than silently corrupting dependent repositories.

### Proof of Concept
1. Create an object pool from repository `A` (`CreateObjectPool`), then link `A` to it via `LinkRepositoryToObjectPool`. `A` now depends on pool objects via `objects/info/alternates`.
2. Without calling `DisconnectGitAlternates` on `A`, call `DeleteObjectPool` on the pool.
3. `repoutil.Remove` deletes the pool's directory tree entirely. [6](#0-5) 
4. Any Git operation against `A` that needs an object only stored in the (now-deleted) pool fails with a missing-object/corruption error, since `A`'s alternates file still points at the removed path.

### Citations

**File:** proto/objectpool.proto (L24-30)
```text
//    the primary object pool member.
// 4. Repositories may leave the object pool via DisconnectGitAlternates. There is not much of a
//    reason to do this for any repositories except for the primary object pool member in case it
//    for example becomes private.
// 5. When the object pool does not have any members anymore it gets deleted via DeleteObjectPool.
//    It is the responsibility of the caller to ensure that it really has no members left, else
//    any existing member will become corrupt.
```

**File:** proto/objectpool.proto (L44-46)
```text
  // DeleteObjectPool deletes the object pool. There are no safety checks in place, so if any
  // repository is still using this object pool it will become corrupted.
  rpc DeleteObjectPool(DeleteObjectPoolRequest) returns (DeleteObjectPoolResponse) {
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

**File:** internal/git/objectpool/disconnect.go (L89-133)
```go
	objectFiles, err := findObjectFiles(altObjectDir)
	if err != nil {
		return err
	}

	repositoryRelativePath, err := filepath.Rel(f.Root(), repoPath)
	if err != nil {
		return fmt.Errorf("repository relative path: %w", err)
	}

	for _, path := range objectFiles {
		sourceRelativePath, err := filepath.Rel(f.Root(), filepath.Join(altObjectDir, path))
		if err != nil {
			return fmt.Errorf("source relative path: %w", err)
		}
		targetRelativePath := filepath.Join(repositoryRelativePath, "objects", path)

		if err := storage.MkdirAll(f, filepath.Dir(targetRelativePath)); err != nil {
			return err
		}

		if err := storage.Link(f, sourceRelativePath, targetRelativePath); err != nil {
			if errors.Is(err, fs.ErrExist) {
				continue
			}

			return err
		}
	}

	if err := transaction.VoteOnContext(ctx, txManager, voting.VoteFromData([]byte("migrate objects")), voting.Committed); err != nil {
		return fmt.Errorf("committed vote for migrating objects: %w", err)
	}

	altFile, err := repo.InfoAlternatesPath(ctx)
	if err != nil {
		return err
	}

	backupFile, err := newBackupFile(altFile)
	if err != nil {
		return err
	}

	return removeAlternatesIfOk(ctx, repo, altFile, backupFile, logger, txManager)
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
