Based on my research, `DeleteObjectPool` is the strongest analog to the `cancelPromotion` bug class: it unconditionally deletes shared state (the object pool repository) without verifying that all dependents (pool members) have finished depending on it, causing unrecoverable, permanent loss of access to objects for anyone still relying on that state — the same shape as `cancelPromotion` wiping `_promotions[_promotionId]` and permanently locking unclaimed rewards.

### Title
DeleteObjectPool destroys shared object storage without checking for still-linked members, permanently corrupting dependent repositories - ([File: internal/gitaly/service/objectpool/delete.go])

### Summary
`DeleteObjectPool` removes an object pool repository from disk with no verification that member repositories are still linked (i.e., still depend on the pool's objects via `objects/info/alternates`). Once deleted, any repository still linked to that pool becomes permanently corrupt because the deduplicated objects it depends on are gone — mirroring the `cancelPromotion` pattern where deleting shared bookkeeping state (`delete _promotions[_promotionId]`) leaves dependents (unclaimed reward epochs) permanently unable to complete their in-progress state.

### Finding Description
`DeleteObjectPool` calls `repoutil.Remove` on the pool with no precondition check for existing members: [1](#0-0) 

The protobuf documentation itself acknowledges the danger explicitly: *"DeleteObjectPool deletes the object pool. There are no safety checks in place, so if any repository is still using this object pool it will become corrupted."* [2](#0-1) 

This is architecturally consistent with how object pools work: member repositories rely on the pool's `objects/info/alternates` file to resolve shared objects, and removing a member from a pool requires carefully re-hard-linking objects back into the member and validating connectivity before removing the alternates link (`objectpool.Disconnect`/`removeAlternatesIfOk`) — precisely because a naive removal of shared object storage breaks dependents: [3](#0-2) [4](#0-3) 

`DeleteObjectPool`, however, performs none of this member-aware disconnection logic — it just calls `repoutil.Remove`, which moves the pool directory into a temp dir and deletes it outright: [5](#0-4) 

The caller (any Rails/GitLab client, or an ordinary user with access to the RPC surface) is solely responsible for ensuring "it really has no members left" — the client-side comment on the RPC makes this responsibility explicit and shifts a correctness-critical invariant to an untrusted caller: [6](#0-5) 

The Praefect-side handler (`DeleteObjectPoolHandler`) similarly forwards the delete to every backing Gitaly node and clears the database record without any member/link verification: [7](#0-6) 

This matches the `cancelPromotion` bug class precisely: an operation that deletes/clears state that other in-flight or dependent processes rely on, with no mechanism to first drain, migrate, or verify that dependents have safely detached — resulting in permanent inaccessibility of resources tied to that state.

### Impact Explanation
Any repository still linked as an object pool member becomes silently and irrecoverably corrupt after `DeleteObjectPool` runs: its Git objects (commits, blobs, trees only stored in the pool) become permanently unreachable, and subsequent Git operations against that repository (`fsck`, fetches, reads, clones) will fail with missing-object errors. Because objects are physically removed from disk with `os.RemoveAll`/`repoutil.Remove` rather than kept around for lazy recovery, there is no way to reconstitute the lost data — this is a permanent DoS/data-loss condition analogous to permanently-locked unclaimed rewards in the original finding.

### Likelihood Explanation
The precondition (no members left) is explicitly called out in the proto docs as being the caller's sole responsibility, with no server-side enforcement at all. This makes the vulnerability reachable through completely ordinary usage: any client mis-ordering `DisconnectGitAlternates`/pool cleanup relative to `DeleteObjectPool`, any race between concurrent RPCs, or any actor with access to the `ObjectPoolService` RPC surface can trigger irreversible corruption of otherwise-healthy fork repositories.

### Recommendation
Before removing the pool's on-disk data, `DeleteObjectPool` should verify there are no remaining linked members (e.g., by checking pool membership tracking or by attempting to enumerate/verify all known forks/alternates references before deletion), and refuse the deletion (or first force-disconnect all members via the existing `objectpool.Disconnect` migration path) rather than deleting the pool unconditionally.

### Proof of Concept
1. Create an object pool from repository A via `CreateObjectPool`.
2. Link repository A (or any fork B) to the pool via `LinkRepositoryToObjectPool`, causing shared objects to be deduplicated out of A/B and to exist only in the pool.
3. Call `DeleteObjectPool` on that pool — observe it succeeds unconditionally per `internal/gitaly/service/objectpool/delete.go` L15-29, with no member check.
4. Attempt to read/fetch objects from repository A/B that were deduplicated into the pool — they are now permanently missing, and `git fsck`/reads fail, mirroring the permanently-unclaimable state left by `cancelPromotion`.

### Citations

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

**File:** internal/git/objectpool/disconnect.go (L24-30)
```go
// Disconnect disconnects the specified repository from its object pool. If the repository does not
// utilize an alternate object database, no error is returned. For repositories that depend on
// alternate objects, the following steps are performed:
//   - Alternate objects are hard-linked to the main repository.
//   - The repository's Git alternates file is backed up and object pool disconnected.
//   - A connectivity check is performed to ensure the repository is complete. If this check fails,
//     the repository is reconnected to the object pool via the backup and an error returned.
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

**File:** internal/gitaly/repoutil/remove.go (L117-134)
```go

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
```

**File:** proto/go/gitalypb/objectpool_grpc.pb.go (L62-64)
```go
	// DeleteObjectPool deletes the object pool. There are no safety checks in place, so if any
	// repository is still using this object pool it will become corrupted.
	DeleteObjectPool(ctx context.Context, in *DeleteObjectPoolRequest, opts ...grpc.CallOption) (*DeleteObjectPoolResponse, error)
```

**File:** internal/praefect/delete_object_pool.go (L18-49)
```go
// DeleteObjectPoolHandler intercepts DeleteObjectPool calls, deletes the database records and
// deletes the object pool from every backing Gitaly node.
func DeleteObjectPoolHandler(rs datastore.RepositoryStore, logger log.Logger, conns Connections) grpc.StreamHandler {
	return removeRepositoryHandler(rs, logger, conns,
		func(stream grpc.ServerStream) (*gitalypb.Repository, error) {
			var req gitalypb.DeleteObjectPoolRequest
			if err := stream.RecvMsg(&req); err != nil {
				return nil, fmt.Errorf("receive request: %w", err)
			}

			repo, err := objectpoolsvc.ExtractPool(&req)
			if err != nil {
				return nil, err
			}

			if !storage.IsRailsPoolRepository(repo) {
				return nil, structerr.NewInvalidArgument("%w", objectpool.ErrInvalidPoolDir)
			}

			return repo, nil
		},
		func(ctx context.Context, conn *grpc.ClientConn, rewritten *gitalypb.Repository) error {
			_, err := gitalypb.NewObjectPoolServiceClient(conn).DeleteObjectPool(ctx, &gitalypb.DeleteObjectPoolRequest{
				ObjectPool: &gitalypb.ObjectPool{
					Repository: rewritten,
				},
			})
			return err
		},
		func() proto.Message { return &gitalypb.DeleteObjectPoolResponse{} },
		false,
	)
```
