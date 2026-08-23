### Title
Unauthorized cross-repository object disclosure via unchecked `LinkRepositoryToObjectPool` - (File: `internal/gitaly/service/objectpool/link.go`)

### Summary
The external report's root cause is that a state-mutating "merge" operation combines the internal state of one entity into another without verifying that the caller is entitled to combine those two entities, letting already-consumed state be re-applied to gain an unearned benefit. `ObjectPoolService.LinkRepositoryToObjectPool` in Gitaly exhibits the same pattern at the RPC layer: it merges a repository's object visibility with an arbitrary, caller-supplied object pool with no verification that the two repositories are actually related (e.g., that the target repository was forked from that pool), permitting any caller who can invoke this RPC field to gain read access to objects held in a pool it does not legitimately belong to.

### Finding Description
`LinkRepositoryToObjectPool` only validates that `repository` is a well-formed, existing repository via `s.locator.ValidateRepository`, then resolves the pool and calls `pool.Link(ctx, repo)`: [1](#0-0) 

`objectpool.Link` itself performs no check that `repo` is a legitimate fork/member of `pool` — it only checks whether the repository is *already* linked to a pool and, if not, unconditionally writes the pool's path into the repository's `objects/info/alternates` file: [2](#0-1) 

Because Git's alternate object directories act as a search path across the entire object store of the pool (loose objects and packs, not limited to reachable refs), linking grants full read access to every object ever fetched into that pool, as documented: [3](#0-2) [4](#0-3) 

The `LinkRepositoryToObjectPoolRequest` proto marks `object_pool` as an `additional_repository` field but Gitaly does not enforce any ownership/fork-network relationship between `repository` (target) and `object_pool` (additional) at the Gitaly layer — this is analogous to the FLUX report's `merge()` combining an already-claimed token's balance into an unrelated token without checking that the caller had legitimate rights over both sides of the merge. Both cases share the same root defect class: a "merge/link" primitive that silently trusts caller-supplied identifiers for both sides of the combination instead of validating an established relationship, letting the caller pull privileged state (unclaimed FLUX balance / pooled Git objects) across a trust boundary.

### Impact Explanation
If an actor can invoke `LinkRepositoryToObjectPool` with a `repository` they control and an `object_pool` relative path belonging to a repository/pool they do not own (object pool relative paths are deterministic, hash-derived from the origin repository's path, so they can be computed/guessed for a target repository), the attacker's repository becomes an alternate-object-directory consumer of the victim pool. Any object ever present in that pool (including objects from commits/branches later made private, deleted, or force-pushed away) becomes readable through the attacker-controlled repository via standard read RPCs, resulting in disclosure of source code, secrets, or history that the attacker was never authorized to view. This matches the "cross-repository object access" impact category.

### Likelihood Explanation
Exploitation only requires the ability to issue a `LinkRepositoryToObjectPoolRequest` with attacker-chosen `repository` and `object_pool` field values — i.e., a crafted RPC field, consistent with an ordinary authenticated Gitaly RPC client. No malicious peer, MITM, or leaked token is needed; the request is a normal, individually-addressable Gitaly RPC and the vulnerable code path (`link.go`) contains no relationship check between the two repositories, so likelihood is high wherever Gitaly's RPC surface is reachable by less-trusted callers than intended (multi-tenant Gitaly deployments, or any caller of the Gitaly API that bypasses the higher-level application's own authorization logic).

### Recommendation
In `LinkRepositoryToObjectPool` (`internal/gitaly/service/objectpool/link.go`) and/or `objectpool.Link` (`internal/git/objectpool/link.go`), verify an established relationship between `repository` and `object_pool` before writing the alternates file — for example, require that the pool was created via `CreateObjectPool` from this specific repository or its recorded fork-network origin, and persist/verify that provenance server-side rather than trusting the RPC caller to supply a correct pairing.

### Proof of Concept
Not independently reproducible from the indexed code alone (no test harness for cross-tenant pool linking was found in the index); the analysis is based on tracing the RPC handler and `Link` implementation, which show no cross-repository ownership check: [1](#0-0) [5](#0-4) 

A conceptual PoC would be:
1. Determine or brute-force the relative path of an existing object pool `P` belonging to a victim repository `V` (pool paths are deterministically derived, per `doc/object_pools.md`).
2. As an unrelated user, create/own repository `A`.
3. Call `LinkRepositoryToObjectPool` with `repository = A`, `object_pool = P`.
4. Observe that `A`'s `objects/info/alternates` now references `P`'s object directory, and that objects from `V`'s history (including ones not reachable via `A`'s own refs) can now be read from `A` via standard object-read RPCs (e.g., `GetBlob`, `CommitService`).

### Citations

**File:** internal/gitaly/service/objectpool/link.go (L10-27)
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

	repo := s.localRepoFactory.Build(repository)

	if err := pool.Link(ctx, repo); err != nil {
		return nil, structerr.NewInternal("%w", err)
	}

	return &gitalypb.LinkRepositoryToObjectPoolResponse{}, nil
```

**File:** internal/git/objectpool/link.go (L28-66)
```go
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

**File:** proto/objectpool.proto (L52-58)
```text
  // LinkRepositoryToObjectPool links the specified repository to the object pool. Objects contained
  // in the object pool will be deduplicated for this repository when repacking objects.
  rpc LinkRepositoryToObjectPool(LinkRepositoryToObjectPoolRequest) returns (LinkRepositoryToObjectPoolResponse) {
    option (op_type) = {
      op: MUTATOR
    };
  }
```
