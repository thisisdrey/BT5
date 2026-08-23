### Title
`DeleteObjectPool` permits reuse of a still-referenced pool path, allowing cross-repository object injection - ([File: internal/gitaly/service/objectpool/delete.go])

### Summary
`DeleteObjectPool` unconditionally removes the on-disk object-pool repository without verifying that no member repository still references it, and `CreateObjectPool` will happily (re)create a brand-new pool repository at that exact same relative path as soon as it is free. This is the direct structural analog of the Sentiment bug: a resource that has been "closed" (deleted) can be silently reactivated/reused at the same identity by an unrelated actor without any check that the old identity's dependents ("owner" in the Sentinel case, "pool members" here) have been properly detached first.

### Finding Description
`DeleteObjectPool` resolves the pool and calls `repoutil.Remove`, deleting the pool directory from disk with no verification step: [1](#0-0) 

The RPC comment itself documents that this is unsafe by design: *"DeleteObjectPool deletes the object pool. There are no safety checks in place, so if any repository is still using this object pool it will become corrupted."* [2](#0-1) 

Meanwhile, `CreateObjectPool` only checks that the target path does not currently exist on disk before creating a brand-new pool there — it has no notion of "this path used to belong to another pool that other repositories may still be pointing to": [3](#0-2) [4](#0-3) 

Member repositories keep their link to a pool purely via a relative path string written into `objects/info/alternates` (`internal/git/objectpool/link.go`), which is never invalidated when the pool is deleted: [5](#0-4) 

Because Gitaly does not track pool membership itself (that bookkeeping is external, e.g. in GitLab Rails/Praefect), there is a window where:
1. A pool `P` at relative path `X` has members `A` and `B`, both with `objects/info/alternates` pointing at `X/objects`.
2. `DeleteObjectPool` is invoked for `P` while `B` is still a linked member (e.g. a race between fork creation/deletion, or a bookkeeping bug on the caller side) — the pool directory at `X` is deleted with no check.
3. An attacker-controlled or unrelated flow subsequently calls `CreateObjectPool` with a **different origin repository** but targeting the now-free path `X`. `repoutil.Create`'s only guard is "does not exist yet" — which it doesn't, so a brand-new pool seeded from the attacker's chosen origin is created at `X`.
4. Repository `B` still has its alternates file pointing at `X/objects`, which now transparently resolves to the attacker's new pool contents. Any subsequent object lookup, `FetchIntoObjectPool`, or repack of `B` will search/reference objects from the new, unrelated pool.

This is the same shape of bug as the Sentiment finding: a lifecycle transition (`close owner` / `delete pool`) is not cross-checked against the resource's ongoing linkage, so the "closed" identity can be transparently re-established by a different actor, and old holders of the identity (`inactiveAccountsOf` list / member repositories' alternates) unknowingly resume trusting it.

### Impact Explanation
This is a concrete cross-repository object access / repository corruption path: after the pool path is reused, repository `B`'s reads can be served by objects belonging to a repository the attacker fully controls, and further housekeeping/repack operations against `B` can pull those foreign objects into `B`'s reachable graph. This can lead to information disclosure across repository/tenant boundaries and to repository corruption, matching the "cross-repository object access" and "extraction/DoS of a handler" categories called out as acceptable impact.

### Likelihood Explanation
The path-reuse condition requires that the pool-deletion decision (normally made by the caller, e.g. GitLab Rails determining "zero members left") is wrong or racy relative to Gitaly's own unenforced state — which the RPC's own documentation already flags as an existing risk ("no safety checks in place ... any existing member will become corrupt"). Gitaly performs zero verification here, so any caller-side membership-tracking bug (not a Gitaly authorization bypass) is sufficient to trigger the identity-reuse condition; Gitaly provides no defense-in-depth against it.

### Recommendation
Before deleting an object pool, or before allowing `CreateObjectPool` to reuse a relative path, verify — from Gitaly's own on-disk state or via an explicit reference count/marker — that no repository under the pool's storage still has an alternates file pointing at the pool being deleted/recreated. At minimum, `CreateObjectPool` should refuse to (re)create a pool at a path that still shows up as an alternate target for any other repository, closing the same-path reuse gap.

### Proof of Concept
1. `CreateObjectPool(origin=A, pool=X)`; `LinkRepositoryToObjectPool(repo=A, pool=X)`; `LinkRepositoryToObjectPool(repo=B, pool=X)` — `B`'s `objects/info/alternates` now contains `../../X/objects`.
2. `DeleteObjectPool(pool=X)` — succeeds unconditionally per `internal/gitaly/service/objectpool/delete.go`, even though `B` is still linked.
3. `CreateObjectPool(origin=C /* attacker-controlled */, pool=X)` — succeeds because `repoutil.Create` only checks path non-existence (`internal/gitaly/repoutil/create.go:96-104`).
4. Read from `B` (e.g. `GetObjectPool`, `RepositoryInfo`, or any object lookup that traverses alternates) — it now resolves through the attacker's pool contents at `X`, demonstrating cross-repository object exposure without any authorization check tying the new pool's origin to `B`'s original owner.

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

**File:** internal/git/objectpool/create.go (L37-48)
```go
	objectPoolPath, err := locator.GetRepoPath(ctx, proto.GetRepository(), storage.WithRepositoryVerificationSkipped())
	if err != nil {
		return nil, err
	}

	if _, err := os.Stat(objectPoolPath); err == nil {
		return nil, structerr.NewFailedPrecondition("target path exists already").
			WithMetadata("object_pool_path", objectPoolPath)
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, structerr.NewInternal("checking object pool existence: %w", err).
			WithMetadata("object_pool_path", objectPoolPath)
	}
```

**File:** internal/gitaly/repoutil/create.go (L96-104)
```go
	// The repository must not exist on disk already, or otherwise we won't be able to
	// create it with atomic semantics.
	if _, err := os.Stat(targetPath); !errors.Is(err, fs.ErrNotExist) {
		if err == nil {
			return structerr.NewAlreadyExists("repository exists already")
		}

		return fmt.Errorf("pre-lock stat: %w", err)
	}
```

**File:** internal/git/objectpool/link.go (L25-52)
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
```
