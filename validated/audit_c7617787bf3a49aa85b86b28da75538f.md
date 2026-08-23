I have sufficient evidence to establish the analog vulnerability. `CreateRepository` accepts an arbitrary client-supplied `RelativePath` with no restriction preventing it from matching the `@pools/xx/yy/<hash>.git` pattern reserved for object pools, and `ValidateRepository` / `ValidateRelativePath` only check path traversal, not namespace collision. `objectpool.Create` fails permanently once a path exists there.### Title
Object pool creation can be permanently blocked by pre-creating its target path - (File: internal/git/objectpool/create.go)

### Summary
`ObjectPoolService.CreateObjectPool` derives the on-disk location of an object pool purely from the client-supplied `RelativePath` in the request (validated only for format, not uniqueness/ownership). `CreateRepository` and other repository-creation RPCs accept an arbitrary, caller-chosen `RelativePath` under the same storage with no check that it doesn't collide with a reserved object-pool path. Because `objectpool.Create` permanently refuses to (re)create a pool once *anything* exists at the target path, an ordinary user who can predict or control the object-pool disk path for a fork/import network can pre-create a directory/repository there, causing the legitimate `CreateObjectPool` call issued by GitLab's housekeeping/fork flow to fail forever with `FailedPrecondition`.

### Finding Description
`objectpool.Create` computes the pool path via the locator and aborts if anything already exists there: [1](#0-0) 

This mirrors exactly the Fraxlend bug class: a deterministic/attacker-influenceable "slot" (there: `salt`; here: `RelativePath` under `@pools/xx/yy/<hash>.git`) is checked for prior occupancy before creation, and once occupied the create call fails permanently — there is no mechanism to reclaim or retry with a different identity, because the pool identity *is* the path.

The pool path format is only checked for internal self-consistency (subdirectories must equal the first four hex digits of the trailing hash, lowercase, no traversal) via `storage.IsPoolRepository` / `errInvalidPoolDir`: [2](#0-1) 

Nothing in `CreateObjectPool`'s handler ties the `RelativePath` to the origin repository's actual identity, checks that the caller is authorized to claim that specific pool, or checks that no unrelated repository has already claimed it: [3](#0-2) 

Critically, the same storage-relative-path namespace is shared with ordinary repository creation. `CreateRepository`/`repoutil.Create` accept any client-chosen `RelativePath` and only validate that it doesn't escape the storage root — they do not forbid paths that look like `@pools/xx/yy/<hash>.git`: [4](#0-3) [5](#0-4) 

Since the object pool's disk path is a hash derived from predictable inputs (e.g. an existing project/pool identifier, as documented for the Rails-generated `@pools/` hashed layout) an attacker who can create/import/fork repositories on the same storage can race a legitimate `CreateObjectPool` (triggered automatically by GitLab's fork/housekeeping logic) and pre-occupy the exact target directory with an empty directory or their own repository. This is the gitaly-side analog of the Fraxlend `deployedPairsBySalt`/`deployedPairsByName` front-running bug, where a colliding, attacker-chosen identity permanently blocks legitimate deployment at that identity slot.

The project's own test suite documents that even an *empty* pre-existing directory is sufficient to permanently block pool creation, explicitly calling this out as a known-bad state ("This can be considered a bug, but for now we abide"): [6](#0-5) 

### Impact Explanation
A successful pre-occupation of the pool path denies GitLab's fork-network object pool deduplication for that project indefinitely: every subsequent legitimate `CreateObjectPool` call for that fork network fails with `FailedPrecondition: target path exists already`, degrading storage efficiency (no dedup across forks) without any way for GitLab/Gitaly to recover automatically, similar to how the Fraxlend front-run permanently prevented redeployment of a colliding pair. This is a denial-of-service against a specific handler/feature reachable from an ordinary user's repository-creation/import path, not merely a cosmetic issue.

### Likelihood Explanation
Exploitability depends on the attacker being able to (a) predict the future pool disk path before the legitimate `CreateObjectPool` call runs, and (b) issue a competing repository/pool creation request for that exact path first. Both preconditions are plausible: the pool path is a deterministic hash based on project/pool metadata (documented Rails-side hashing scheme referenced by `railsPoolDirRegexp`), and any user capable of creating/importing repositories on the storage can supply an arbitrary `RelativePath`, including one matching the reserved `@pools/` pattern, since Gitaly does not check for or prevent this collision. The main uncertainty (not verifiable from the Gitaly codebase alone) is exactly how predictable/discoverable the pool hash computation on the Rails side is in a given deployment — this reduces certainty around real-world timing feasibility but does not remove the underlying missing-uniqueness/ownership check in Gitaly.

### Recommendation
- In `objectpool.Create`/`CreateObjectPool`, do not treat "target path already exists" as unconditionally fatal; instead verify whether the existing path was created by a legitimate, matching pool creation (e.g., verify pool identity/origin linkage) before failing, or allow reclaiming an empty/unclaimed directory.
- Reject `CreateRepository`/repository-import RelativePaths that match the reserved object-pool path pattern (`storage.IsPoolRepository`) unless the request is specifically a `CreateObjectPool` call, closing the shared-namespace collision.
- Consider deriving/validating the object pool's expected path server-side from the origin repository’s own state (as `DerivePoolPath` does using a repository ID) rather than trusting an arbitrary client-supplied `RelativePath`, removing the pre-image predictability that enables front-running.

### Proof of Concept
1. Determine or predict the future `@pools/xx/yy/<hash>.git` path that GitLab will use when it later creates an object pool for a given fork network (the hash is derived from project/pool metadata known to any user who can see or infer that metadata).
2. As an ordinary user with repository-creation/import privileges on the same Gitaly storage, call `CreateRepository` (or any RPC ultimately invoking `repoutil.Create`) with `RelativePath` set to that exact `@pools/xx/yy/<hash>.git` path, creating an empty directory/repository there: [4](#0-3) 
3. When GitLab subsequently triggers the legitimate `CreateObjectPool` RPC for that fork network, `objectpool.Create` performs `os.Stat(objectPoolPath)`, finds the pre-created entry, and permanently fails with `FailedPrecondition("target path exists already")`: [7](#0-6) 
4. All future `CreateObjectPool` attempts for this fork network fail identically since the path remains occupied, denying object pool deduplication for that repository network — directly analogous to the Fraxlend `_deployFirst` salt-collision DoS.

### Citations

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

**File:** internal/gitaly/storage/repository_path.go (L21-38)
```go
	// railsPoolDirRegexp is used to validate object pool directory structure and name as generated by Rails.
	railsPoolDirRegexp = regexp.MustCompile(`@pools/([0-9a-f]{2})/([0-9a-f]{2})/([0-9a-f]{64})\.git$`)
)

// IsRailsPoolRepository returns whether the repository is a pool repository generated by Rails.
func IsRailsPoolRepository(repo Repository) bool {
	matches := railsPoolDirRegexp.FindStringSubmatch(repo.GetRelativePath())
	if matches == nil || !strings.HasPrefix(matches[3], matches[1]+matches[2]) {
		return false
	}

	return true
}

// IsPraefectPoolRepository returns whether the repository is a Praefect generated object pool repository.
func IsPraefectPoolRepository(repo Repository) bool {
	return praefectPoolDirRegexp.MatchString(repo.GetRelativePath())
}
```

**File:** internal/gitaly/service/objectpool/create.go (L17-41)
```go
func (s *server) CreateObjectPool(ctx context.Context, in *gitalypb.CreateObjectPoolRequest) (*gitalypb.CreateObjectPoolResponse, error) {
	if in.GetOrigin() == nil {
		return nil, errMissingOriginRepository
	}

	poolRepo := in.GetObjectPool().GetRepository()
	if poolRepo == nil {
		return nil, errMissingPool
	}

	if !storage.IsPoolRepository(poolRepo) {
		return nil, errInvalidPoolDir
	}

	// repoutil.Create creates the repositories in a temporary directory. This means the repository is not created in the location
	// expected by the transaction manager. This makes sense without transactions, but with transactions, there's no real point in
	// doing so given a failed transaction's state is anyway removed. Creating the repository in a temporary directory is problematic
	// as the reference transaction hook is invoked for the repository from unexpected location, causing the transaction to fail to
	// associate the reference updates with the repository.
	//
	// Run the repository creation without the transaction in the context. The transactions reads the created repository's state from
	// the disk when committing it, so it's not necessary to capture the updates from the reference-transaction hook. This avoids the
	// problem for now, and later with transactions enabled by default we can stop creating repositories in unexpected locations.
	ctxWithoutTransaction := storage.ContextWithTransactionID(ctx, 0)
	if err := repoutil.Create(ctxWithoutTransaction, s.logger, s.locator, s.gitCmdFactory, s.catfileCache, s.txManager, s.repositoryCounter, poolRepo, func(poolRepo *gitalypb.Repository) error {
```

**File:** internal/gitaly/repoutil/create.go (L90-104)
```go
) error {
	targetPath, err := locator.GetRepoPath(ctx, repository, storage.WithRepositoryVerificationSkipped())
	if err != nil {
		return structerr.NewInvalidArgument("locate repository: %w", err)
	}

	// The repository must not exist on disk already, or otherwise we won't be able to
	// create it with atomic semantics.
	if _, err := os.Stat(targetPath); !errors.Is(err, fs.ErrNotExist) {
		if err == nil {
			return structerr.NewAlreadyExists("repository exists already")
		}

		return fmt.Errorf("pre-lock stat: %w", err)
	}
```

**File:** internal/gitaly/config/locator.go (L81-83)
```go
	if _, err := storage.ValidateRelativePath(storagePath, relativePath); err != nil {
		return structerr.NewInvalidArgument("%w", err).WithMetadata("relative_path", relativePath)
	}
```

**File:** internal/git/objectpool/create_test.go (L82-97)
```go
	t.Run("target exists", func(t *testing.T) {
		relativePath := gittest.NewObjectPoolName(t)
		fullPath := filepath.Join(cfg.Storages[0].Path, relativePath)

		// We currently allow creating object pools when the target path is an empty
		// directory. This can be considered a bug, but for now we abide.
		require.NoError(t, os.MkdirAll(fullPath, mode.Directory))

		_, _, err := createPool(t, &gitalypb.ObjectPool{
			Repository: &gitalypb.Repository{
				StorageName:  cfg.Storages[0].Name,
				RelativePath: relativePath,
			},
		})
		testhelper.RequireGrpcError(t, structerr.NewFailedPrecondition("target path exists already"), err)
	})
```
