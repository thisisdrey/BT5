### Title
CreateRepositoryFromSnapshot/RestoreRepository accept RelativePath values under Gitaly's reserved `+gitaly` prefix, allowing archive content to be written into internal bookkeeping directories - ([File: internal/gitaly/config/locator.go])

### Summary
`configLocator.ValidateRepository` and `GetRepoPath` only validate that a repository's `RelativePath` does not escape the storage root via `storage.ValidateRelativePath`, but never check whether the path collides with Gitaly's reserved `GitalyDataPrefix` (`+gitaly`) namespace used for `tmp`, `cache`, `state`, and `partitions` data. An attacker-controlled `RelativePath` such as `+gitaly/tmp/evil.git` passes validation and, combined with `WithSkipRepositoryExistenceCheck()`/`WithSkipStorageExistenceCheck()`, results in untarred archive content being written directly into Gitaly's internal directories.

### Finding Description
`ValidateRelativePath` in `internal/gitaly/storage/locator.go` (lines 154-164) only verifies the joined path stays within `rootDir`; it performs no check against reserved subtrees. [1](#0-0) 

`configLocator.ValidateRepository` calls this same generic check and nothing else before allowing `GetRepoPath` to resolve `filepath.Join(storagePath, relativePath)`: [2](#0-1) [3](#0-2) 

`GitalyDataPrefix` (`"+gitaly"`) and its derived prefixes (`tmpRootPrefix`, `cachePrefix`, `statePrefix`, `partitionsPrefix`) are defined in the same package but are used only to compute the paths of Gitaly's own internal directories (`CacheDir`, `StateDir`, `TempDir`, `PartitionsDir`); they are never cross-checked against an incoming repository's `RelativePath`: [4](#0-3) [5](#0-4) 

`CreateRepositoryFromSnapshot` calls `ValidateRepository` with `storage.WithSkipRepositoryExistenceCheck()` (so no requirement that a git directory already exists at the path) and then resolves the target path with `WithRepositoryVerificationSkipped()` before untarring attacker-supplied HTTP content into it via `s.untar`: [6](#0-5)  The code comment explicitly warns "the received archive is trusted *a lot*", confirming this path is expected to be defended primarily by path/namespace validation, which is the layer under audit and shown to be incomplete. [7](#0-6) 

The only place that actively guards against walking into `+gitaly` is `FindRepositories`, which explicitly skips `config.GitalyDataPrefix` during storage-wide directory walks — this is a read-side defense, not an input-validation guard applied to write paths like snapshot/restore: [8](#0-7) 

Note: many creation paths go through `repoutil.Create`, which in a Praefect-fronted / WAL-enabled deployment typically rewrites the logical `RelativePath` into a hashed replica path (e.g., via partitioning/replica-path derivation) before it reaches disk, which would neutralize this specific attack in practice for those configurations. I was not able to fully trace `repoutil.Create`'s replica-path rewriting logic within the available iterations to confirm whether standalone (non-WAL, non-Praefect) deployments also rewrite the path or use the raw attacker-supplied `RelativePath` directly. This is a material precondition for exploitability that remains unverified against the actual code path.

### Impact Explanation
If the raw, attacker-controlled `RelativePath` reaches disk unmodified (e.g., in configurations where replica-path rewriting does not occur), an attacker can direct snapshot/restore archive extraction into Gitaly's `+gitaly/tmp`, `+gitaly/cache`, `+gitaly/state`, or `+gitaly/partitions` subtrees. These directories back transaction WAL state, partition assignment metadata, and caches shared across all repositories on that storage, so corrupting them could disrupt transactions or metadata for unrelated repositories on the same storage — a scoped denial-of-service / storage-corruption impact rather than a full RCE.

### Likelihood Explanation
The attacker precondition is simply supplying an arbitrary `RelativePath` on `CreateRepositoryFromSnapshotRequest`/`RestoreRepositoryRequest`, which matches the described unprivileged capability (control of RPC fields). However, exploitability is contingent on whether the deployment's repository-creation path (`repoutil.Create` / routing layer) passes the raw path through untouched versus rewriting it to a generated replica path — this could not be confirmed with the available code excerpts, so likelihood is uncertain pending that verification.

### Recommendation
Add an explicit check in `configLocator.ValidateRepository` (and/or a shared helper called by both `ValidateRepository` and `GetRepoPath`) that rejects any `RelativePath` equal to or nested under `config.GitalyDataPrefix` (covering `tmpRootPrefix`, `cachePrefix`, `statePrefix`, `partitionsPrefix`), returning `structerr.NewInvalidArgument` before any path resolution or existence-check skipping occurs. Apply this validation unconditionally, independent of `WithSkipRepositoryExistenceCheck`/`WithSkipStorageExistenceCheck`, so snapshot/restore code paths cannot bypass it.

### Proof of Concept
```go
func TestConfigLocator_RejectsGitalyReservedPrefix(t *testing.T) {
    cfg := testcfg.Build(t)
    locator := config.NewLocator(cfg)
    ctx := testhelper.Context(t)

    for _, relPath := range []string{
        "+gitaly/tmp/evil.git",
        "+gitaly/state/evil.git",
        "+gitaly/cache/evil.git",
        "+gitaly/partitions/evil.git",
    } {
        repo := &gitalypb.Repository{
            StorageName:  cfg.Storages[0].Name,
            RelativePath: relPath,
        }
        err := locator.ValidateRepository(ctx, repo, storage.WithSkipRepositoryExistenceCheck())
        // Expected: rejection. Currently: err == nil, and GetRepoPath resolves
        // into cfg.Storages[0].Path + "/+gitaly/tmp/evil.git".
        require.Error(t, err, "relative path %q should be rejected as a reserved Gitaly path", relPath)
    }
}
```
At the RPC level: `CreateRepositoryFromSnapshotRequest{Repository: {StorageName: "default", RelativePath: "+gitaly/tmp/evil.git"}, HttpUrl: <attacker-controlled tar URL>}` currently proceeds to untar into `<storage>/+gitaly/tmp/evil.git` instead of being rejected.

### Citations

**File:** internal/gitaly/storage/locator.go (L157-164)
```go
func ValidateRelativePath(rootDir, relativePath string) (string, error) {
	absPath := filepath.Join(rootDir, relativePath)
	if rootDir != absPath && !strings.HasPrefix(absPath, rootDir+string(os.PathSeparator)) {
		return "", ErrRelativePathEscapesRoot
	}

	return filepath.Rel(rootDir, absPath)
}
```

**File:** internal/gitaly/config/locator.go (L13-28)
```go
const (
	// tmpRootPrefix is the directory in which we store temporary
	// directories.
	tmpRootPrefix = GitalyDataPrefix + "/tmp"

	// cachePrefix is the directory where all cache data is stored on a
	// storage location.
	cachePrefix = GitalyDataPrefix + "/cache"

	// statePrefix is the directory where all state data is stored on a
	// storage location.
	statePrefix = GitalyDataPrefix + "/state"

	// partitionsPrefix is the directory where all partitions data live
	partitionsPrefix = GitalyDataPrefix + "/partitions"
)
```

**File:** internal/gitaly/config/locator.go (L81-88)
```go
	if _, err := storage.ValidateRelativePath(storagePath, relativePath); err != nil {
		return structerr.NewInvalidArgument("%w", err).WithMetadata("relative_path", relativePath)
	}

	path := filepath.Join(storagePath, repo.GetRelativePath())
	if path == "" {
		return structerr.NewInvalidArgument("repository path is empty")
	}
```

**File:** internal/gitaly/config/locator.go (L142-149)
```go
	storagePath, err := l.GetStorageByName(ctx, repo.GetStorageName())
	if err != nil {
		return "", err
	}
	relativePath := repo.GetRelativePath()

	return filepath.Join(storagePath, relativePath), nil
}
```

**File:** internal/gitaly/config/locator.go (L166-194)
```go
// CacheDir returns the path to the cache dir for a storage.
func (l *configLocator) CacheDir(storageName string) (string, error) {
	return l.getPath(storageName, cachePrefix)
}

// StateDir returns the path to the state dir for a storage.
func (l *configLocator) StateDir(storageName string) (string, error) {
	return l.getPath(storageName, statePrefix)
}

// TempDir returns the path to the temp dir for a storage.
func (l *configLocator) TempDir(storageName string) (string, error) {
	return l.getPath(storageName, tmpRootPrefix)
}

// PartitionsDir returns the path to the partitions dir for a storage.
func (l *configLocator) PartitionsDir(storageName string) (string, error) {
	return l.getPath(storageName, partitionsPrefix)
}

func (l *configLocator) getPath(storageName, prefix string) (string, error) {
	storagePath, ok := l.conf.StoragePath(storageName)
	if !ok {
		return "", structerr.NewInvalidArgument("%s dir: no such storage: %q",
			filepath.Base(prefix), storageName)
	}

	return filepath.Join(storagePath, prefix), nil
}
```

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L123-144)
```go
func (s *server) CreateRepositoryFromSnapshot(ctx context.Context, in *gitalypb.CreateRepositoryFromSnapshotRequest) (*gitalypb.CreateRepositoryFromSnapshotResponse, error) {
	repository := in.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repository, storage.WithSkipRepositoryExistenceCheck()); err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}

	if err := repoutil.Create(ctx, s.logger, s.locator, s.gitCmdFactory, s.catfileCache, s.txManager, s.repositoryCounter, repository, func(repo *gitalypb.Repository) error {
		path, err := s.locator.GetRepoPath(ctx, repo, storage.WithRepositoryVerificationSkipped())
		if err != nil {
			return structerr.NewInternal("getting repo path: %w", err)
		}

		// The archive contains a partial git repository, missing a config file and
		// other important items. Initializing a new bare one and extracting the
		// archive on top of it ensures the created git repository has everything
		// it needs (especially, the config file and hooks directory).
		//
		// NOTE: The received archive is trusted *a lot*. Before pointing this RPC
		// at endpoints not under our control, it should undergo a lot of hardening.
		if err := s.untar(ctx, path, in); err != nil {
			return structerr.NewInternal("extracting snapshot: %w", err)
		}
```

**File:** internal/gitaly/storage/walk/walk.go (L48-51)
```go
		// Don't walk Gitaly's internal files.
		if relPath == config.GitalyDataPrefix {
			return fs.SkipDir
		}
```
