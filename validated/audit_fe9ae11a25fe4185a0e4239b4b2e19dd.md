### Title
Object-directory ownership check bypassed by "quarantine"-named directory heuristic - (File: internal/git/localrepo/paths.go)

### Summary
`Repo.ObjectDirectoryPath()` resolves the on-disk object directory for a repository from the caller-supplied `GitObjectDirectory` field of the `Repository` message. Instead of always verifying that a non-standard object directory actually belongs to the target repository, it short-circuits that verification whenever the resolved path's basename happens to be literally named `quarantine` (or its parent is `quarantine` with a `tmp_objdir` prefix). This is structurally the same flaw as the Foundation NFT bug: a single attacker-influenced signal (`recipients[i] == seller` there, `baseDir == "quarantine"` here) is used as a proxy for "this is trusted/internal", and when it matches, the code takes an entirely different branch that skips the real ownership/ownership-boundary check.

### Finding Description
`ObjectDirectoryPath` in [1](#0-0)  takes `repo.GetGitObjectDirectory()` — a field of the `Repository` protobuf that is directly settable by any gRPC caller of `RepositoryService.GetObjectDirectorySize` (marked `[(target_repository)=true]` but with no field-level validation of `git_object_directory`) [2](#0-1) [3](#0-2) .

The function first validates only that the path stays inside the *storage root* (not the specific repository): [4](#0-3) 

It then computes `baseDir`/`parentDir` of that storage-relative path and treats it as a legitimate "transaction quarantine directory" purely by name: [5](#0-4) 

Only when `isTransactionQuarantineDir` is **false** does the code perform the real security check — confirming the path either lives inside the repo itself, or, if outside, that it is prefixed by `expectedQuarantinePrefix`, a value derived from a SHA1 hash of *this specific repository's* relative path (`storage.QuarantineDirectoryPrefix`) [6](#0-5) [7](#0-6) . That per-repo prefix check is precisely the mechanism meant to guarantee cross-repository isolation, analogous to the "calculate owner/seller revenue separately" fix Foundation implemented. But if the attacker can arrange for the resolved directory's basename to literally be `quarantine`, this entire ownership check is skipped, and execution falls straight through to: [8](#0-7) 

`fullPath` is simply `filepath.Join(repoPath, objectDirectoryPath)`; since `ValidateRelativePath` upstream only constrained the path to the *storage* root and not the *repository* root, `objectDirectoryPath` can contain `..` segments that walk out of the caller's own repository into another repository's tree elsewhere in the same storage, as long as the final path segment is named `quarantine`. Existing regression tests already demonstrate the intended protection this bypass defeats — e.g. `TestGetObjectDirectorySize_quarantine` explicitly checks that swapping in another repo's *real* quarantine directory is rejected via the prefix check [9](#0-8)  — but that protection only runs on the `!isTransactionQuarantineDir` branch, which the name-based heuristic lets an attacker avoid entirely.

### Impact Explanation
An attacker who can invoke `GetObjectDirectorySize` (or any other RPC that calls `ObjectDirectoryPath`) with a crafted `Repository.git_object_directory` field pointing to `.../<attacker-reachable-dir-named-"quarantine">` can make Gitaly compute a full path outside the boundaries of the caller's own repository while completely bypassing the per-repository ownership check. This is a cross-repository storage-isolation escape: the RPC will happily stat/walk and report on-disk information (sizes) belonging to a directory that does not belong to the requesting repository, undermining the storage-path isolation guarantees the codebase explicitly documents and tests for elsewhere (`storage.ValidateRelativePath`, `QuarantineDirectoryPrefix`).

### Likelihood Explanation
- The vulnerable field, `GitObjectDirectory`, is a normal request field of the public `Repository` message, reachable from any authenticated Gitaly client (the same trust level as ordinary push/fetch/access-check traffic, not a privileged internal caller) — this matches the "crafted RPC field" attack surface explicitly allowed by the scan rules.
- The bypass condition (`baseDir == "quarantine"`) requires only that *some* directory reachable via a storage-root-relative path ends in the literal name `quarantine` — trivial to engineer (e.g. any repository/pool path containing such a subdirectory).
- No malicious peer, MITM, leaked token, or elevated privilege is required — only crafting one field of a standard RPC request.

### Recommendation
Never let a name-based heuristic (`basename == "quarantine"`) substitute for the actual ownership check. Always validate that a non-default `GitObjectDirectory`/quarantine-looking path is prefixed by the repository-specific `expectedQuarantinePrefix` (or is inside the repository itself), regardless of whether the path's basename looks like a quarantine directory. If the "transaction quarantine" fast path must exist for the internal transaction manager, only apply it for internally-generated quarantine directories (e.g., recognize them via an unforgeable server-side channel rather than a client-suppliable string), and fold the real prefix/ownership verification into that path as well.

### Proof of Concept
1. Repository A (attacker-controlled) exists on the storage, e.g. `relative_path = "attacker/repo.git"`.
2. Attacker arranges for a directory literally named `quarantine` to exist somewhere reachable via a storage-root-relative path — e.g., another repository `victim/repo.git` that has (or can be made to have) an object/data subdirectory ending in `quarantine`, or the attacker's own repo contains such a path that, via `..` traversal, resolves into the victim's tree while keeping storage-root containment satisfied by `ValidateRelativePath(storagePath, …)`.
3. Attacker calls:
```
GetObjectDirectorySize(Repository{
  storage_name: "default",
  relative_path: "attacker/repo.git",
  git_object_directory: "../../victim/repo.git/some/nested/quarantine",
})
```
4. In `ObjectDirectoryPath`, `storage.ValidateRelativePath(storagePath, filepath.Join(repoPath, objectDirectoryPath))` succeeds (still within storage root) [4](#0-3) .
5. `baseDir := filepath.Base(relativeObjectDirectoryPath)` equals `"quarantine"`, so `isTransactionQuarantineDir = true` [10](#0-9) , and the per-repo `expectedQuarantinePrefix` check is skipped entirely [6](#0-5) .
6. `fullPath` resolves inside `victim/repo.git`; `os.Stat` succeeds, and `GetObjectDirectorySize` walks and sums file sizes there, returning cross-repository disk-usage data to the attacker [11](#0-10) .

Note: I was unable to fully verify the concrete GitLab-side directory-naming constraints (e.g., whether GitLab's repo-path scheme guarantees the presence of a literally-`quarantine`-named subdirectory reachable from an attacker's own repo without server-side sanitization), since that depends on deployment/path-generation conventions not fully covered in the indexed code. This should be confirmed by directly testing against a running Gitaly instance with a Devin session with filesystem/RPC access, as suggested by the note on index coverage limits.

### Citations

**File:** internal/git/localrepo/paths.go (L19-52)
```go
// ObjectDirectoryPath returns the full path of the object directory. The errors returned are gRPC
// errors with relevant error codes and should be passed back to gRPC without further decoration.
func (repo *Repo) ObjectDirectoryPath(ctx context.Context) (string, error) {
	repoPath, err := repo.Path(ctx)
	if err != nil {
		return "", err
	}

	objectDirectoryPath := repo.GetGitObjectDirectory()
	if objectDirectoryPath == "" {
		return "", structerr.NewInvalidArgument("object directory path is not set")
	}

	storagePath, err := repo.locator.GetStorageByName(ctx, repo.GetStorageName())
	if err != nil {
		return "", fmt.Errorf("get storage by name: %w", err)
	}

	// Ensure the path points somewhere in the storage.
	relativeObjectDirectoryPath, err := storage.ValidateRelativePath(storagePath, filepath.Join(repoPath, objectDirectoryPath))
	if err != nil {
		return "", structerr.NewInvalidArgument("validate relative path: %w", err)
	}

	parentDir := filepath.Base(filepath.Dir(relativeObjectDirectoryPath))
	baseDir := filepath.Base(relativeObjectDirectoryPath)
	isTransactionQuarantineDir := (baseDir == "quarantine") || ((parentDir == "quarantine") && strings.HasPrefix(baseDir, "tmp_objdir"))

	// Transactions quarantine a repository by pointing the object directory to a 'quarantine' named
	// directory in the transaction's temporary directory. If the base directory is `quarantine`,
	// Git push may apply an additional layer of quarantine such as `/quarantine/tmp_objdir-incoming-Gbc29N`
	// so we don't assert the `/quarantine` being the last element of the path. We thus also check for
	// whether the parent directory is in `quarantine` and whether the base directory has the expected
	// `tmp_objdir` suffix.
```

**File:** internal/git/localrepo/paths.go (L53-75)
```go
	if !isTransactionQuarantineDir {
		// We need to check whether the relative object directory as given by the repository is
		// a valid path. This may either be a path in the Git repository itself, where it may either
		// point to the main object directory storage or to an object quarantine directory as
		// created by git-receive-pack(1). Alternatively, if that is not the case, then it may be a
		// manual object quarantine directory located in the storage's temporary directory. These
		// have a repository-specific prefix which we must check in order to determine whether the
		// quarantine directory does in fact belong to the repo at hand.
		if _, origError := storage.ValidateRelativePath(repoPath, objectDirectoryPath); origError != nil {
			tempDir, err := repo.locator.TempDir(repo.GetStorageName())
			if err != nil {
				return "", structerr.NewInvalidArgument("getting storage's temporary directory: %w", err)
			}

			expectedQuarantinePrefix := filepath.Join(tempDir, storage.QuarantineDirectoryPrefix(repo))
			absoluteObjectDirectoryPath := filepath.Join(repoPath, objectDirectoryPath)

			// The relative path is outside of the repository
			if !strings.HasPrefix(absoluteObjectDirectoryPath, expectedQuarantinePrefix) {
				return "", structerr.NewInvalidArgument("not a valid relative path: %w", origError)
			}
		}
	}
```

**File:** internal/git/localrepo/paths.go (L77-83)
```go
	fullPath := filepath.Join(repoPath, objectDirectoryPath)
	if _, err := os.Stat(fullPath); os.IsNotExist(err) {
		return "", structerr.NewNotFound("object directory does not exist: %q", fullPath)
	}

	return fullPath, nil
}
```

**File:** proto/repository.proto (L1274-1279)
```text
// GetObjectDirectorySizeRequest is a request for the GetObjectDirectorySize RPC.
message GetObjectDirectorySizeRequest {
  // repository is the repo to query. The storage_name and relative_path attributes
  // must be provided.
  Repository repository = 1 [(target_repository)=true];
}
```

**File:** internal/gitaly/service/repository/size.go (L39-56)
```go
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
```

**File:** internal/gitaly/storage/locator.go (L201-212)
```go
// QuarantineDirectoryPrefix returns a prefix for use in the temporary directory. The prefix is
// based on the relative repository path and will stay stable for any given repository. This allows
// us to verify that a given quarantine object directory indeed belongs to the repository at hand.
// Ideally, this function would directly be located in the quarantine module, but this is not
// possible due to cyclic dependencies.
func QuarantineDirectoryPrefix(repo Repository) string {
	hash := [20]byte{}
	if repo != nil {
		hash = sha1.Sum([]byte(repo.GetRelativePath()))
	}
	return fmt.Sprintf("quarantine-%x-", hash[:8])
}
```

**File:** internal/gitaly/service/repository/size_test.go (L278-317)
```go
	t.Run("quarantined repo with different relative path", func(t *testing.T) {
		repo1, _ := gittest.CreateRepository(t, ctx, cfg)
		quarantine1, cleanup1, err := quarantine.New(ctx, gittest.RewrittenRepository(t, ctx, cfg, repo1), logger, locator)
		require.NoError(t, err)
		t.Cleanup(cleanup1)

		repo2, _ := gittest.CreateRepository(t, ctx, cfg)
		quarantine2, cleanup2, err := quarantine.New(ctx, gittest.RewrittenRepository(t, ctx, cfg, repo2), logger, locator)
		require.NoError(t, err)
		t.Cleanup(cleanup2)

		// We swap out the the object directories of both quarantines. So while both are
		// valid, we still expect that this RPC call fails because we detect that the
		// swapped-in quarantine directory does not belong to our repository.
		repo := proto.Clone(quarantine1.QuarantinedRepo()).(*gitalypb.Repository)
		repo.GitObjectDirectory = quarantine2.QuarantinedRepo().GetGitObjectDirectory()
		// quarantine.New in Gitaly would receive an already rewritten repository. Gitaly would then calculate
		// the quarantine directories based on the rewritten relative path. That quarantine would then be looped
		// through Rails, which would then send a request with the quarantine object directories set based on the
		// rewritten relative path but with the original relative path of the repository. Since we're using the production
		// helpers here, we need to manually substitute the rewritten relative path with the original one when sending
		// it back through the API.
		repo.RelativePath = repo1.GetRelativePath()

		// Rails sends the repository's relative path from the access checks as provided by Gitaly. If transactions are enabled,
		// this is the snapshot's relative path. Include the metadata in the test as well as we're testing requests with quarantine
		// as if they were coming from access checks. The RPC is also a special case as it only works with a quarantine set.
		ctx := metadata.AppendToOutgoingContext(ctx, storagemgr.MetadataKeySnapshotRelativePath,
			// Gitaly sends the snapshot's relative path to Rails from `pre-receive` and Rails
			// sends it back to Gitaly when it performs requests in the access checks. The repository
			// would have already been rewritten by Praefect, so we have to adjust for that as well.
			gittest.RewrittenRepository(t, ctx, cfg, repo).GetRelativePath(),
		)

		response, err := client.GetObjectDirectorySize(ctx, &gitalypb.GetObjectDirectorySizeRequest{
			Repository: repo,
		})
		require.Error(t, err, "rpc error: code = InvalidArgument desc = GetObjectDirectoryPath: relative path escapes root directory")
		require.Nil(t, response)
	})
```
