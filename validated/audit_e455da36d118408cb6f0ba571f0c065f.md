### Title
Missing storage-scope validation in `LinkRepositoryToObjectPool` allows cross-storage alternates linking and unauthorized object access - ([File: internal/gitaly/service/objectpool/link.go])

### Summary
`LinkRepositoryToObjectPool` writes an `objects/info/alternates` file that grants a repository read-access to every object in the specified pool. Unlike the sibling `FetchIntoObjectPool` RPC, this handler never checks that the target `repository` and the `object_pool` reside in the same storage, so a caller can link a repository to an object pool located in an entirely different storage.

### Finding Description
The `LinkRepositoryToObjectPool` handler only validates that `repository` is a valid Gitaly repository and that `object_pool` is a valid pool repository — it performs no check that the two share a storage: [1](#0-0) 

By contrast, its sibling RPC `FetchIntoObjectPoolRequest` explicitly enforces this invariant: [2](#0-1) 

The underlying `Link` function computes the alternates content purely via `filepath.Rel` between the two repository paths, with no bound to the repository's own storage root: [3](#0-2) 

This relative path is written directly into `objects/info/alternates` and committed: [4](#0-3) 

Because `filepath.Rel` will happily produce a `../../../other-storage/...` style path when `pool` and `repo` live under different storage roots, the resulting alternates file causes Git to search for objects outside the repository's own storage, in an object pool that may belong to a different project/tenant. This is the same class of flaw as the reported `RollerPeriphery.approve()` bug: a state-mutating operation accepts two independently-controlled parameters (`spender`/`token` there, `object_pool`/`repository` here) without validating that the caller is authorized to link them together, letting one entity's assets/objects be exposed to another.

### Impact Explanation
A successful link exposes every object stored in the target object pool to any client that can subsequently read the linked repository (e.g., via `CommitService`/`BlobService`), even though those objects may belong to a completely unrelated repository/storage the caller has no legitimate access to. This is a cross-repository/cross-storage object disclosure, matching the "cross-repository object access" acceptance criterion.

### Likelihood Explanation
`LinkRepositoryToObjectPool` is a `MUTATOR`-annotated RPC reachable by any client holding a valid Gitaly auth token (the same trust level as most other repository/object-pool RPCs); the request's `object_pool` and `repository` fields are attacker-influenceable protobuf fields with no cross-field storage validation, unlike the neighboring `FetchIntoObjectPool` RPC which was hardened for exactly this case. The lack of parity between the two RPCs indicates the check was simply omitted rather than intentionally excluded.

### Recommendation
Add the same storage-name equality check used in `validateFetchIntoObjectPoolRequest` to `LinkRepositoryToObjectPool` before calling `pool.Link`, rejecting requests where `req.GetRepository().GetStorageName() != req.GetObjectPool().GetRepository().GetStorageName()`. Additionally, harden `getRelativeObjectPath`/`Link` in `internal/git/objectpool/link.go` to validate (via `storage.ValidateRelativePath` or equivalent) that the computed relative alternates path stays within the repository's own storage root, independent of the gRPC-layer check.

### Proof of Concept
1. Configure Gitaly with two storages, `storage-a` (containing `repo-a`) and `storage-b` (containing a pool `@pools/xx/yy/pool.git` created from a private repository in `storage-b`).
2. Using a valid Gitaly auth token, call:
   ```
   LinkRepositoryToObjectPool(
     repository: { storage_name: "storage-a", relative_path: "repo-a.git" },
     object_pool: { repository: { storage_name: "storage-b", relative_path: "@pools/xx/yy/pool.git" } }
   )
   ```
3. Because no storage match is enforced, `Link` succeeds and writes an `objects/info/alternates` file in `repo-a.git` containing a relative path that escapes `storage-a` and points into `storage-b`'s pool object directory.
4. Any subsequent read RPC (e.g. `GetBlob`, `TreeEntry`) against `repo-a` for an object ID that only exists in the `storage-b` pool now succeeds, confirming cross-storage object disclosure.

### Citations

**File:** internal/gitaly/service/objectpool/link.go (L10-28)
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
}
```

**File:** internal/gitaly/service/objectpool/fetch_into_object_pool.go (L102-118)
```go
func validateFetchIntoObjectPoolRequest(req *gitalypb.FetchIntoObjectPoolRequest) error {
	if req.GetOrigin() == nil {
		return errors.New("origin is empty")
	}

	if req.GetObjectPool() == nil {
		return errors.New("object pool is empty")
	}

	originRepository, poolRepository := req.GetOrigin(), req.GetObjectPool().GetRepository()

	if originRepository.GetStorageName() != poolRepository.GetStorageName() {
		return errors.New("origin has different storage than object pool")
	}

	return nil
}
```

**File:** internal/git/objectpool/link.go (L54-70)
```go
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

	if err := transaction.CommitLockedFile(ctx, txManager, alternatesWriter); err != nil {
		return fmt.Errorf("committing alternates: %w", err)
	}
```

**File:** internal/git/objectpool/link.go (L149-166)
```go
func getRelativeObjectPath(ctx context.Context, pool, repo *localrepo.Repo) (string, error) {
	poolPath, err := pool.Path(ctx)
	if err != nil {
		return "", fmt.Errorf("getting object pool path: %w", err)
	}

	repoPath, err := repo.Path(ctx)
	if err != nil {
		return "", fmt.Errorf("getting repository path: %w", err)
	}

	relPath, err := filepath.Rel(filepath.Join(repoPath, "objects"), poolPath)
	if err != nil {
		return "", err
	}

	return filepath.Join(relPath, "objects"), nil
}
```
