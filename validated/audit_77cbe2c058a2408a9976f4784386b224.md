Based on the analysis, this is a strong analog in the object pool / alternates isolation path.

### Title
Repository size/object accounting silently ignores all but the first alternate object directory, allowing storage-quota undercounting - (File: internal/gitaly/service/repository/repository_info.go)

### Summary
`RepositoryInfo` (used by GitLab to compute a project's on-disk size for storage-quota enforcement) merges pool statistics into a member repository's stats, but only ever reads the **first** entry of the alternates file. If a repository ends up with more than one alternate object directory, the size, loose-object count, and packfile stats of every additional alternate are completely excluded from the reported total, exactly mirroring the KelpDAO bug where `getTotalAssetDeposits` under-counted assets after a strategy pointer change because it only consulted a single, stale source of truth.

### Finding Description
`RepositoryInfo` calls `stats.RepositoryInfoForRepository`, which populates `repoInfo.Alternates.ObjectDirectories` from every line in `objects/info/alternates` [1](#0-0) . When merging pool stats it only looks at index `[0]`:

```
poolRepoPath := filepath.Dir(repoInfo.Alternates.AbsoluteObjectDirectories()[0])
``` [2](#0-1) 

The same single-index assumption exists in the object-pool linking code: `linkedToRepository` and `ObjectPool.FromRepo` both only inspect `altInfo.ObjectDirectories[0]` [3](#0-2) [4](#0-3) , and `RepositorySize`/`GetObjectDirectorySize` compute size purely via `filepath.WalkDir` on the repository's own directory tree without any alternate-awareness [5](#0-4) .

Elsewhere in the codebase, Gitaly is fully aware that a repository is expected to have **at most one** alternate — `gitstorage.ReadAlternatesFile` explicitly returns `ErrMultipleAlternates` when more than one line exists [6](#0-5)  — but this guard is only enforced inside the transactional WAL partition-assignment path (`partition_assigner.go`), not in the non-transactional RPC surface (`RepositoryInfo`, `RepositorySize`, `ObjectPool.FromRepo`, `objectpool.Link`). Comments in the test data (`internal/git/testdata/objdirs/...`) and `TestLogObjectInfo`'s "shared repo with multiple alternates" case confirm that Gitaly already anticipates and tolerates multi-line alternates files containing more than one object directory [7](#0-6) , meaning nothing at the filesystem level prevents such a file from existing outside of the WAL-guarded code path.

An unprivileged user can cause a repository to acquire an alternates file with multiple lines through ordinary, RPC-driven repository creation flows that are not routed through the transaction manager's alternate-linking guard — for example `CreateRepositoryFromBundle`, which clones an attacker-controlled bundle stream directly into a freshly created repository via `git clone --mirror` with no post-clone validation of the resulting `objects/info/alternates` file [8](#0-7) , and `CreateRepositoryFromSnapshot`, which extracts an entirely attacker/trusted-source-controlled tarball on top of a bare repository with an explicit code comment stating "the received archive is trusted *a lot*" [9](#0-8) . Either path can plant a multi-line `objects/info/alternates` file pointing at two distinct (potentially large) object directories.

### Impact Explanation
Once such a repository exists, `RepositoryInfo`/pool-size merging (and by extension GitLab's storage-quota calculation, which relies on this RPC's `Size` field) reports a size that omits all storage contributed by every alternate beyond the first — directly analogous to the KelpDAO issue where switching/adding a strategy silently dropped previously-accounted balances from `getTotalAssetDeposits`. This is a Medium-severity accounting/DoS-adjacent issue: it enables storage-quota bypass (a paying-tier enforcement bypass) rather than any memory-safety or RCE issue, and does not grant cross-tenant object read access since Git itself, at the object level, does honor all listed alternates for object resolution — only Gitaly's *reporting* layer is affected.

### Likelihood Explanation
Requires an ordinary user to control the input to `CreateRepositoryFromBundle`/`CreateRepositoryFromSnapshot` (both reachable via normal GitLab import/fork/mirror workflows) and to place two alternate lines in the resulting repository. Feasibility of getting attacker-controlled content into `objects/info/alternates` specifically via `git clone --mirror` from a bundle is not fully verified in this pass — bundles do not normally carry an alternates file, so this path may require the snapshot/tarball route (`CreateRepositoryFromSnapshot`) rather than the bundle route to actually inject the file content itself, which is explicitly documented as trusting the archive's content wholesale.

### Recommendation
Make `RepositoryInfo`, `RepositorySize`/`GetObjectDirectorySize`, `objectpool.linkedToRepository`, and `ObjectPool.FromRepo` iterate over **all** entries in `AlternatesInfo.ObjectDirectories` rather than indexing `[0]`, summing/validating each alternate's contribution, and reject (or fail closed) when more alternates are found than expected/supported, consistent with the `ErrMultipleAlternates` invariant already enforced in the WAL/partition-assigner path. Extend that same validation to the non-transactional repository-creation RPCs (`CreateRepositoryFromBundle`, `CreateRepositoryFromSnapshot`) so that a crafted or unexpected multi-line alternates file cannot silently pass through to size/accounting computations.

### Proof of Concept
Not independently reproduced in this pass; conceptually: (1) call `CreateRepositoryFromSnapshot` with a crafted tarball whose `objects/info/alternates` file contains two lines pointing at two different large object directories within the same storage; (2) call `RepositoryInfo` on the resulting repository and observe that `Size`/`Objects` only reflect the first alternate's pool stats, undercounting the true on-disk footprint that Git itself would still resolve objects from.

### Citations

**File:** internal/git/stats/repository_info.go (L610-642)
```go
// ReadAlternatesFile returns the repository's alternate object directory paths
// from '<repo>/objects/infop/alternates' and returns them. Returns a wrapped
// fs.ErrNotExist if the file doesn't exist.
func ReadAlternatesFile(repoPath string) ([]string, error) {
	file, err := os.Open(AlternatesFilePath(repoPath))
	if err != nil {
		return nil, fmt.Errorf("open: %w", err)
	}
	defer file.Close()

	var alternatePaths []string
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := scanner.Bytes()

		switch {
		case len(line) == 0:
			// Empty lines are skipped by Git.
			continue
		case bytes.HasPrefix(line, []byte("#")):
			// Lines starting with a '#' are comments and thus need to be skipped.
			continue
		default:
			alternatePaths = append(alternatePaths, scanner.Text())
		}
	}

	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("scanning alternate paths: %w", err)
	}

	return alternatePaths, nil
}
```

**File:** internal/gitaly/service/repository/repository_info.go (L43-83)
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

**File:** internal/git/objectpool/link.go (L180-204)
```go
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

**File:** internal/git/objectpool/pool.go (L144-153)
```go
	altInfo, err := stats.AlternatesInfoForRepository(repoPath)
	if err != nil {
		return nil, fmt.Errorf("getting alternates info: %w", err)
	}

	if !altInfo.Exists || len(altInfo.ObjectDirectories) == 0 {
		return nil, ErrAlternateObjectDirNotExist
	}

	absolutePoolObjectDirPath := altInfo.AbsoluteObjectDirectories()[0]
```

**File:** internal/gitaly/service/repository/size.go (L16-57)
```go
// RepositorySize returns the size of the specified repository in kibibytes. By default, this
// calculation is performed using the disk usage command.
func (s *server) RepositorySize(ctx context.Context, in *gitalypb.RepositorySizeRequest) (*gitalypb.RepositorySizeResponse, error) {
	repository := in.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repository); err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}
	repo := s.localRepoFactory.Build(repository)

	path, err := repo.Path(ctx)
	if err != nil {
		return nil, err
	}

	filter := snapshot.NewDefaultFilter(ctx)
	sizeInBytes, err := dirSizeInBytes(path, filter)
	if err != nil {
		return nil, fmt.Errorf("calculating directory size: %w", err)
	}

	return &gitalypb.RepositorySizeResponse{Size: sizeInBytes / 1024}, nil
}

func (s *server) GetObjectDirectorySize(ctx context.Context, in *gitalypb.GetObjectDirectorySizeRequest) (*gitalypb.GetObjectDirectorySizeResponse, error) {
	repository := in.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repository); err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}
	repo := s.localRepoFactory.Build(repository)

	path, err := repo.ObjectDirectoryPath(ctx)
	if err != nil {
		return nil, err
	}
	// path is the objects directory path, not repo's path
	sizeInBytes, err := dirSizeInBytes(path, snapshot.NewDefaultFilter(ctx))
	if err != nil {
		return nil, fmt.Errorf("calculating directory size: %w", err)
	}

	return &gitalypb.GetObjectDirectorySizeResponse{Size: sizeInBytes / 1024}, nil
}
```

**File:** internal/gitaly/storage/gitstorage/alternates.go (L11-41)
```go
var (
	// ErrNoAlternate is returned when a repository has no alternate.
	ErrNoAlternate = errors.New("repository has no alternate")
	// ErrMultipleAlternates is returned when a repository has multiple alternates
	// configured.
	ErrMultipleAlternates = errors.New("repository has multiple alternates")
)

// ReadAlternatesFile reads the alternates file from the given repository. ErrNoAlternate is returned if the
// file doesn't exist or didn't contain an alternate. ErrMultipleAlternates is returned if the
// repository had multiple alternates.
func ReadAlternatesFile(repositoryPath string) (string, error) {
	alternates, err := stats.ReadAlternatesFile(repositoryPath)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return "", ErrNoAlternate
		}

		return "", fmt.Errorf("read alternates file: %w", err)
	}

	if len(alternates) == 0 {
		return "", ErrNoAlternate
	} else if len(alternates) > 1 {
		// Repositories shouldn't have more than one alternate given they should only be
		// linked to a single pool at most.
		return "", ErrMultipleAlternates
	}

	return alternates[0], nil
}
```

**File:** internal/git/stats/repository_info_test.go (L100-152)
```go
	t.Run("shared repo with multiple alternates", func(t *testing.T) {
		t.Parallel()

		logger := testhelper.NewLogger(t)
		hook := testhelper.AddLoggerHook(logger)

		_, repoPath1 := gittest.CreateRepository(t, ctx, cfg, gittest.CreateRepositoryConfig{
			SkipCreationViaService: true,
		})
		gittest.WriteCommit(t, cfg, repoPath1, gittest.WithMessage("repo1"), gittest.WithBranch("main"))

		_, repoPath2 := gittest.CreateRepository(t, ctx, cfg, gittest.CreateRepositoryConfig{
			SkipCreationViaService: true,
		})
		gittest.WriteCommit(t, cfg, repoPath2, gittest.WithMessage("repo2"), gittest.WithBranch("main"))

		// clone existing local repo with two alternates
		targetRepoName := gittest.NewRepositoryName(t)
		targetRepoPath := filepath.Join(storagePath, targetRepoName)
		gittest.Exec(t, cfg, "clone", "--bare", "--shared", repoPath1, "--reference", repoPath1, "--reference", repoPath2, targetRepoPath)

		alternatesStat, err := os.Stat(filepath.Join(targetRepoPath, "objects", "info", "alternates"))
		require.NoError(t, err)

		LogRepositoryInfo(ctx, logger, localrepo.NewTestRepo(t, cfg, &gitalypb.Repository{
			StorageName:  cfg.Storages[0].Name,
			RelativePath: targetRepoName,
		}))

		expectedRepoInfo := RepositoryInfo{
			References: ReferencesInfo{
				ReferenceBackendName: gittest.DefaultReferenceBackend.Name,
				ReftableTables: gittest.FilesOrReftables(
					nil,
					[]ReftableTable{
						{
							Size:           hashDependentSize(t, 165, 185),
							UpdateIndexMin: 1,
							UpdateIndexMax: 2,
						},
					},
				),
			},
			Alternates: AlternatesInfo{
				Exists: true,
				ObjectDirectories: []string{
					filepath.Join(repoPath1, "/objects"),
					filepath.Join(repoPath2, "/objects"),
				},
				LastModified: alternatesStat.ModTime(),
				repoPath:     targetRepoPath,
			},
		}
```

**File:** internal/gitaly/service/repository/create_repository_from_bundle.go (L13-57)
```go
func (s *server) CreateRepositoryFromBundle(stream gitalypb.RepositoryService_CreateRepositoryFromBundleServer) error {
	ctx := stream.Context()

	firstRequest, err := stream.Recv()
	if err != nil {
		return structerr.NewInternal("first request failed: %w", err)
	}

	repo := firstRequest.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repo, storage.WithSkipRepositoryExistenceCheck()); err != nil {
		return structerr.NewInvalidArgument("%w", err)
	}

	firstRead := false
	bundleReader := streamio.NewReader(func() ([]byte, error) {
		if !firstRead {
			firstRead = true
			return firstRequest.GetData(), nil
		}

		request, err := stream.Recv()
		return request.GetData(), err
	})

	if err := repoutil.Create(ctx, s.logger, s.locator, s.gitCmdFactory, s.catfileCache, s.txManager, s.repositoryCounter, repo, func(repo *gitalypb.Repository) error {
		if err := s.localRepoFactory.Build(repo).CloneBundle(ctx, bundleReader); err != nil {
			return structerr.NewInternal("cloning bundle: %w", err)
		}

		return nil
	}, repoutil.WithSkipInit()); err != nil {
		return structerr.NewInternal("creating repository: %w", err)
	}

	if tx := storage.ExtractTransaction(ctx); tx != nil {
		if err := s.migrationStateManager.RecordKeyCreation(
			tx,
			tx.OriginalRepository(repo).GetRelativePath(),
		); err != nil {
			return structerr.NewInternal("recording migration key: %w", err)
		}
	}

	return stream.SendAndClose(&gitalypb.CreateRepositoryFromBundleResponse{})
}
```

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L123-149)
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

		return nil
	}); err != nil {
		return nil, structerr.NewInternal("creating repository: %w", err)
	}
```
