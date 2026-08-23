## Title
Argument injection into custom Git hooks via unvalidated `GitObjectDirectory`/`GitAlternateObjectDirectories` in quarantine gating - (File: `internal/gitaly/hook/custom.go`)

## Summary
`NewHooksPayload` copies `GitObjectDirectory`/`GitAlternateObjectDirectories` straight from whatever `storage.Repository` value is supplied by the caller into the `HooksPayload.Repo` that is later handed to custom hooks [1](#0-0) . When the pre-receive hook builds the environment for administrator-installed custom hooks, it turns these fields directly into `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES` via a bare `filepath.Join(repoPath, ...)`, with none of the traversal/ownership checks that the equivalent, security-sensitive `localrepo.Repo.ObjectDirectoryPath()` performs.

## Finding Description
`localrepo.Repo.ObjectDirectoryPath()` is the "trusted" path resolver for a repository's (possibly quarantined) object directory. It explicitly validates that the relative object directory either resolves inside the repository/storage root via `storage.ValidateRelativePath`, or, if it is a quarantine directory, that it matches the expected `quarantine-<hash>-` prefix derived from the repository's own relative path [2](#0-1) . This is proven by dedicated tests rejecting `../bazqux.git`, `/../bazqux.git`, and mismatched quarantine directories with `codes.InvalidArgument` [3](#0-2)  and by `GetObjectDirectorySize` rejecting a "swapped" quarantine that doesn't belong to the target repo [4](#0-3) .

However, the analogous code path that builds the **custom hook execution environment** does not perform this validation. `customHooksEnv()` takes `payload.Repo.GetGitObjectDirectory()` / `GetGitAlternateObjectDirectories()` and does a raw `filepath.Join(repoPath, ...)`, then injects the result straight into `GIT_OBJECT_DIRECTORY` / `GIT_ALTERNATE_OBJECT_DIRECTORIES` for the custom `pre-receive`/`update`/`post-receive` hook subprocess, with no call to `storage.ValidateRelativePath`, no quarantine-prefix check, and no repository-ownership check [5](#0-4) .

The `Repo` fields consumed here (`GitObjectDirectory`, `GitAlternateObjectDirectories`) are ordinary fields of the shared `Repository` protobuf message, and `NewHooksPayload` populates the hook payload's `Repo` by directly copying them from whatever repository value the RPC handler passes in [1](#0-0) . This mirrors the class of bug in the reference report: a value (`_collectionArtistAddress`/here `GitObjectDirectory`) is trusted and baked into a security-relevant, hard-to-reverse artifact (an irrevocable artist signature/here, the quarantine object-directory search path fed to installed custom hooks) via a code path that omits the validation performed elsewhere in the same codebase for the "canonical" version of the same operation (`setCollectionData`'s missing `> 0` check / `ObjectDirectoryPath`'s missing use in `customHooksEnv`). Just as the NextGen bug let an admin bind a signature to the wrong (fake) artist address before finalizing the real one, this Gitaly analog lets whichever component supplies the hook's `Repository` message bind the custom-hook's object-directory search path to an unvalidated location before/independent of the strict path Gitaly uses for its own trusted RPCs.

## Impact Explanation
If `GitObjectDirectory`/`GitAlternateObjectDirectories` can be set to a value that escapes the intended quarantine/repository boundary (e.g. containing `../` segments or pointing at another repository's object store) by the time it reaches `customHooksEnv`, administrator-installed custom hooks would be executed with a Git object search path pointing outside of what Gitaly's own quarantine gating guarantees. This breaks the same invariant that `ObjectDirectoryPath()` is designed to enforce elsewhere in the codebase (that a repository's object directory environment always resolves to a path Gitaly can prove belongs to that repository or its legitimate quarantine), and could let a custom hook observe/interact with objects from a directory it should not have access to (cross-repository object exposure via a hook/quarantine-gating bypass).

## Likelihood Explanation
Medium: the field-level access is limited to code paths that route a `Repository` message through `NewHooksPayload` into `customHooksEnv`, which is reachable whenever pre-receive/update/post-receive custom hooks run (i.e., on every push processed through `git-receive-pack` or `UpdaterWithHooks.UpdateReference`, both of which are ordinary push/write flows). The root cause is architectural: this is the one place in the quarantine/hook subsystem where the object-directory field is trusted without running it through the same validation logic (`storage.ValidateRelativePath` + quarantine-prefix check) that every other consumer of `GitObjectDirectory` uses, as directly demonstrated by the divergence between `internal/git/localrepo/paths.go` and `internal/gitaly/hook/custom.go`.

## Recommendation
In `customHooksEnv()` (`internal/gitaly/hook/custom.go`), do not build `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES` via a bare `filepath.Join`. Instead, route the same validation used by `localrepo.Repo.ObjectDirectoryPath()` (relative-path validation against the storage root and quarantine-prefix verification against `storage.QuarantineDirectoryPrefix(repo)`) before injecting these values into the custom hook's environment, so custom hooks can never be pointed at object directories that the hook manager cannot prove belong to the target repository or its own quarantine.

## Proof of Concept
Exact exploitability could not be fully confirmed within the scope of this review because it depends on which upstream RPC/internal caller ultimately supplies the `storage.Repository` value passed into `NewHooksPayload` for a given push flow, and whether any of those callers forward a client-influenced `GitObjectDirectory`/`GitAlternateObjectDirectories` value unmodified rather than one freshly computed by `quarantine.Apply` (which is always safe). This would need to be validated with a live Devin session tracing all `NewHooksPayload(...)`/`PreReceiveHook`/`UpdateHook` call sites to determine whether any accept an externally influenced `Repository.GitObjectDirectory` value before it reaches `customHooksEnv`. The code-level discrepancy itself (missing validation in `customHooksEnv` compared to `ObjectDirectoryPath`) is confirmed directly from the source shown above.

### Citations

**File:** internal/git/gitcmd/hooks_payload.go (L159-167)
```go
	return HooksPayload{
		Repo: &gitalypb.Repository{
			StorageName:                   repo.GetStorageName(),
			RelativePath:                  repo.GetRelativePath(),
			GitObjectDirectory:            repo.GetGitObjectDirectory(),
			GitAlternateObjectDirectories: repo.GetGitAlternateObjectDirectories(),
			GlRepository:                  repo.GetGlRepository(),
			GlProjectPath:                 repo.GetGlProjectPath(),
		},
```

**File:** internal/git/localrepo/paths.go (L37-75)
```go
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

**File:** internal/git/localrepo/paths_test.go (L120-178)
```go
		{
			desc: "object directory path points outside of storage",
			repo: repoWithGitObjDir(quarantinedRepo, func() string {
				escapingPath, err := filepath.Rel(repoPath, filepath.Dir(cfg.Storages[0].Path))
				require.NoError(t, err)
				return escapingPath
			}()),
			err: codes.InvalidArgument,
		},
		{
			desc: "no GitObjectDirectoryPath",
			repo: repoProto,
			err:  codes.InvalidArgument,
		},
		{
			desc: "with directory traversal",
			repo: repoWithGitObjDir(repoProto, "../bazqux.git"),
			err:  codes.InvalidArgument,
		},
		{
			desc: "valid path but doesn't exist",
			repo: repoWithGitObjDir(repoProto, "foo../bazqux.git"),
			err:  codes.NotFound,
		},
		{
			desc: "with sneaky directory traversal",
			repo: repoWithGitObjDir(repoProto, "/../bazqux.git"),
			err:  codes.InvalidArgument,
		},
		{
			desc: "with traversal outside repository",
			repo: repoWithGitObjDir(repoProto, "objects/../.."),
			err:  codes.InvalidArgument,
		},
		{
			desc: "with traversal outside repository with trailing separator",
			repo: repoWithGitObjDir(repoProto, "objects/../../"),
			err:  codes.InvalidArgument,
		},
		{
			desc: "with deep traversal at the end",
			repo: repoWithGitObjDir(repoProto, "bazqux.git/../.."),
			err:  codes.InvalidArgument,
		},
		{
			desc: "quarantined repo",
			repo: quarantinedRepo,
			path: filepath.Join(repoPath, quarantinedRepo.GetGitObjectDirectory()),
		},
		{
			desc: "quarantined repo with parent directory",
			repo: repoWithGitObjDir(quarantinedRepo, quarantinedRepo.GetGitObjectDirectory()+"/.."),
			err:  codes.InvalidArgument,
		},
		{
			desc: "quarantined repo with directory traversal",
			repo: repoWithGitObjDir(quarantinedRepo, quarantinedRepo.GetGitObjectDirectory()+"/../foobar.git"),
			err:  codes.InvalidArgument,
		},
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

**File:** internal/gitaly/hook/custom.go (L172-190)
```go
	objectDirectory := env.ExtractValue(envs, "GIT_OBJECT_DIRECTORY")
	if objectDirectory == "" && payload.Repo.GetGitObjectDirectory() != "" {
		objectDirectory = filepath.Join(repoPath, payload.Repo.GetGitObjectDirectory())
	}
	if objectDirectory != "" {
		customEnvs = append(customEnvs, "GIT_OBJECT_DIRECTORY="+objectDirectory)
	}

	alternateObjectDirectories := env.ExtractValue(envs, "GIT_ALTERNATE_OBJECT_DIRECTORIES")
	if alternateObjectDirectories == "" && len(payload.Repo.GetGitAlternateObjectDirectories()) != 0 {
		var absolutePaths []string
		for _, alternateObjectDirectory := range payload.Repo.GetGitAlternateObjectDirectories() {
			absolutePaths = append(absolutePaths, filepath.Join(repoPath, alternateObjectDirectory))
		}
		alternateObjectDirectories = strings.Join(absolutePaths, ":")
	}
	if alternateObjectDirectories != "" {
		customEnvs = append(customEnvs, "GIT_ALTERNATE_OBJECT_DIRECTORIES="+alternateObjectDirectories)
	}
```
