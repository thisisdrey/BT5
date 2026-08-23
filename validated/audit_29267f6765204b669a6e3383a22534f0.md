Confirmed: `DeleteObjectPool` has no membership check before removal — the RPC docstring itself states "There are no safety checks in place, so if any repository is still using this object pool it will become corrupted," and the implementation in `internal/gitaly/service/objectpool/delete.go` calls `repoutil.Remove` unconditionally on the pool once resolved via `poolForRequest`, with no verification that pool members (fork-network repositories relying on the pool's shared objects via `objects/info/alternates`) still depend on it.

### Title
Unconditional object pool deletion corrupts all linked fork-network repositories - (File: internal/gitaly/service/objectpool/delete.go)

### Summary
`DeleteObjectPool` mirrors the reported Vault pattern: a caller-invocable, unprivileged-by-RPC-scope action (`withdraw()`/`DeleteObjectPool`) unconditionally consumes/destroys a shared resource that other actors (future transactions/other repositories) still depend on, with no state check gating the destructive action.

### Finding Description
Object pools deduplicate shared Git objects across a fork network via the `objects/info/alternates` mechanism [1](#0-0) . `DeleteObjectPool` resolves the pool and immediately calls `repoutil.Remove` to delete it from disk, without checking whether any member repository is still linked to it [2](#0-1) . The RPC's own documentation acknowledges this: "There are no safety checks in place, so if any repository is still using this object pool it will become corrupted" [3](#0-2) [4](#0-3) . Because dissociating a member from a pool requires an explicit `DisconnectGitAlternates` step that hard-links pool objects back into the member before the alternates file is removed [5](#0-4) , any member that has not gone through that safe disconnection still resolves objects purely via the (now-deleted) pool's alternate directory. Deleting the pool out from under linked members is directly analogous to the reported bug class: a caller can trigger destruction of a resource that other, unrelated operations still rely on, with no protection against premature/concurrent consumption.

### Impact Explanation
Once the pool is deleted while members remain linked, every fork-network repository still pointing at it via `objects/info/alternates` immediately becomes unable to resolve objects that were deduplicated into the pool, causing `git-fsck`/read/fetch/clone failures for those repositories — a storage-level corruption and denial of service affecting all members of the fork network, not just the caller's own repository.

### Likelihood Explanation
`DeleteObjectPool` is a normal Mutator RPC in `ObjectPoolService` exposed like other Gitaly RPCs [3](#0-2) , reachable by any caller authorized to operate on the object-pool repository. No membership check exists before deletion, so the race/ordering mistake (or intentional misuse) requires no special privilege beyond normal repository/object-pool operation rights, and can be triggered at any time relative to the lifecycle of pool members, including immediately after `LinkRepositoryToObjectPool` and before any member calls `DisconnectGitAlternates`.

### Recommendation
Before removing the pool in `DeleteObjectPool`, verify there are no remaining linked members (e.g., track pool membership, or scan/validate absence of dependent alternates), and reject the deletion (or refuse and require explicit force) if members are still linked — mirroring the fix direction of gating destructive shared-resource actions on the actual state of dependents, rather than leaving it to caller discipline as noted in the RPC's own "no safety checks" caveat.

### Proof of Concept
1. Create repository `origin`, call `CreateObjectPool` to create pool `P`, then `LinkRepositoryToObjectPool` to link `origin` (or a fork) to `P` — `origin`'s objects/info/alternates now points at `P` and its duplicate objects are removed on next repack, per the housekeeping flow in `doc/object_pools.md`.
2. Without calling `DisconnectGitAlternates`, call `DeleteObjectPool` against `P`. `internal/gitaly/service/objectpool/delete.go` deletes `P` unconditionally.
3. Attempt to read/fetch/clone `origin`: objects that were deduplicated into `P` are unreadable because the alternates directory no longer exists, reproducing the "repository will become corrupted" outcome documented in `proto/objectpool.proto`.

### Citations

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

**File:** proto/go/gitalypb/objectpool_grpc.pb.go (L62-64)
```go
	// DeleteObjectPool deletes the object pool. There are no safety checks in place, so if any
	// repository is still using this object pool it will become corrupted.
	DeleteObjectPool(ctx context.Context, in *DeleteObjectPoolRequest, opts ...grpc.CallOption) (*DeleteObjectPoolResponse, error)
```
