### Title
`RepositoryInfo` merges pool-repository disk-usage accounting from an unvalidated alternates path - ([File: internal/gitaly/service/repository/repository_info.go])

### Summary
`RepositoryInfo` derives an object-pool "member" bonus by reading the repository's `objects/info/alternates` file and deriving a pool repository path from it, then folding that pool's size/loose-object/packfile statistics into the response *without* validating that the resolved alternate path actually refers to a legitimate, linked object pool. This mirrors the reported Treasury `invest` bug: a numeric/indirect reference (there, `collateralIndex`; here, the alternates-derived path) is trusted and used to pull in accounting data for a second resource without checking that it is the expected/authorized one, corrupting the aggregate accounting result.

### Finding Description
`RepositoryInfo` computes the repository's own on-disk size, then checks `repoInfo.Alternates.Exists` and, if so, resolves `poolRepoPath` from the *first* absolute alternate object directory taken straight from the repository's `objects/info/alternates` file: [1](#0-0) 

Unlike `objectpool.FromRepo`, which after computing the same kind of pool path explicitly calls `locator.ValidateRepository(...)` and rejects the pool if it is not a proper repository (returning `ErrInvalidPoolRepository`): [2](#0-1) 

`repository_info.go`'s `RepositoryInfo` handler performs **no equivalent check**. It simply computes `poolRelativePath` via `filepath.Rel(storagePath, poolRepoPath)` and builds a `poolRepo` from it, then unconditionally calls `stats.LooseObjectsInfoForRepository`, `stats.PackfilesInfoForRepository`, and `dirSizeInBytes` on that path and merges/adds the results into the caller-visible response: [3](#0-2) 

Because the alternates file is read verbatim (via `stats.RepositoryInfoForRepository` → alternates parsing) with no verification that the target is (a) actually a pool this repository is linked to by Gitaly's own object-pool bookkeeping, or (b) even a valid git repository, the size/loose-object/packfile numbers folded into `RepositoryInfoResponse` can be attributable to any directory the alternates file happens to point at. This is the same missing-validation pattern as the Treasury bug: a value obtained from a request-adjacent, less-trusted source (`collateralIndex` there; the on-disk `objects/info/alternates` content here) is used to pull a second resource into an accounting computation with no check that it matches the expected linked resource.

### Impact Explanation
The `Size`, `Objects`, and related counters returned by `RepositoryInfo` are consumed for storage-quota accounting and repository housekeeping decisions (the RPC is explicitly documented in `proto/repository.proto` as a size-accounting RPC). If a repository's alternates file can end up referencing a directory other than its true linked pool — e.g., through repository replication/fork flows that copy or partially reconstruct `objects/info/alternates` (`internal/git/objectpool/link.go`, `internal/git/quarantine/quarantine.go`) or via any inconsistency between the on-disk alternates file and the actual object-pool linkage tracked by Gitaly — the reported size can silently include (or omit) another repository's data. This causes exactly the class of accounting/PnL-style error described in the report: values reported for one resource are computed using data belonging to a different, unverified resource, which can mislead quota enforcement, storage billing, or housekeeping decisions built on top of `RepositoryInfo`.

### Likelihood Explanation
Likelihood is Low-to-Medium. The `objects/info/alternates` file is not directly attacker-writable through the normal `git push`/quarantine path, since Gitaly's quarantine mechanism restricts what an ordinary push can write. However, the alternates file's *content* is under-checked wherever it is read for accounting purposes (unlike `objectpool.FromRepo`, which does validate it), so any code path that can produce a repository with a stale, mismatched, or manipulated alternates entry (e.g., partial replication, fork edge cases, or a repository state left over from disconnection/relinking) will silently feed unrelated directory data into the size computation — with no error, no logging, and no rejection.

### Recommendation
- **Short term:** In `RepositoryInfo` (`internal/gitaly/service/repository/repository_info.go`), before merging pool stats, validate the derived `poolRepo` the same way `objectpool.FromRepo` does — call `locator.ValidateRepository` (and/or `storage.IsPoolRepository`) on the resolved pool repository, and verify the resolved path stays within the storage root (mirroring the boundary checks already used in `internal/git/localrepo/paths.go`'s `ObjectDirectoryPath`). Reject or skip the pool-merge step if validation fails, rather than unconditionally summing its stats.
- **Long term:** Centralize "resolve alternates → validated pool repository" logic in a single trusted helper (reusing `objectpool.FromRepo`) so every consumer of alternates-derived pool information — accounting, housekeeping, disconnection — applies the same validation instead of each call site re-deriving the path independently with varying levels of rigor.

### Proof of Concept
Not independently reproducible from static analysis alone: constructing a concrete on-disk state where a repository's `objects/info/alternates` diverges from its actual pool linkage requires exercising replication/fork/disconnect edge cases end-to-end (e.g., partial `Link`/`Disconnect` failures in `internal/git/objectpool/link.go` and `disconnect.go`), which is outside what can be confirmed via code reading alone. The code-level gap — `RepositoryInfo` merging pool accounting without the validation performed in `objectpool.FromRepo` — is confirmed by direct comparison of the two call sites cited above.

### Citations

**File:** internal/gitaly/service/repository/repository_info.go (L43-61)
```go
	// If the repository is linked to an object pool, collect pool stats and merge them in so
	// that the response reflects the complete stats.
	if repoInfo.Alternates.Exists && len(repoInfo.Alternates.AbsoluteObjectDirectories()) > 0 {
		poolRepoPath := filepath.Dir(repoInfo.Alternates.AbsoluteObjectDirectories()[0])

		storagePath, err := s.locator.GetStorageByName(ctx, request.GetRepository().GetStorageName())
		if err != nil {
			return nil, fmt.Errorf("getting storage path: %w", err)
		}

		poolRelativePath, err := filepath.Rel(storagePath, poolRepoPath)
		if err != nil {
			return nil, fmt.Errorf("computing pool relative path: %w", err)
		}

		poolRepo := s.localRepoFactory.Build(&gitalypb.Repository{
			StorageName:  request.GetRepository().GetStorageName(),
			RelativePath: poolRelativePath,
		})
```

**File:** internal/gitaly/service/repository/repository_info.go (L63-83)
```go
		poolLooseObjects, err := stats.LooseObjectsInfoForRepository(ctx, poolRepo, time.Now().Add(stats.StaleObjectsGracePeriod))
		if err != nil {
			return nil, fmt.Errorf("deriving pool loose objects info: %w", err)
		}

		poolPackfiles, err := stats.PackfilesInfoForRepository(ctx, poolRepo)
		if err != nil {
			return nil, fmt.Errorf("deriving pool packfiles info: %w", err)
		}

		poolSize, err := dirSizeInBytes(poolRepoPath, filter)
		if err != nil {
			return nil, fmt.Errorf("calculating pool repository size: %w", err)
		}

		repoSize += poolSize
		repoInfo = mergePoolInfo(repoInfo, stats.RepositoryInfo{
			LooseObjects: poolLooseObjects,
			Packfiles:    poolPackfiles,
		})
	}
```

**File:** internal/git/objectpool/pool.go (L159-168)
```go
	objectPoolProto := &gitalypb.ObjectPool{
		Repository: &gitalypb.Repository{
			StorageName:  repo.GetStorageName(),
			RelativePath: filepath.Dir(relativePoolObjectDirPath),
		},
	}

	if locator.ValidateRepository(ctx, objectPoolProto.GetRepository()) != nil {
		return nil, ErrInvalidPoolRepository
	}
```
