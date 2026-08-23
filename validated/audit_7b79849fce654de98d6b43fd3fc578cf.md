### Title
Naming-based quarantine bypass allows a crafted `GitObjectDirectory` to point at an unrelated quarantine directory elsewhere in the storage - (File: internal/git/localrepo/paths.go)

### Summary
`Repo.ObjectDirectoryPath` is supposed to accept a caller-supplied `GitObjectDirectory` only if it is either (a) a path inside the repository itself, or (b) a manually created quarantine directory whose name is bound to *this specific repository* via a SHA1-derived prefix (`storage.QuarantineDirectoryPrefix`). However, a third, unauthenticated branch exists purely based on the *basename* of the path (`"quarantine"` or a `tmp_objdir*` child of a directory named `"quarantine"`) that skips all ownership verification.

<cite repo="Kohvert/gitaly--013" path="internal/git/localrepo/paths.go" start="43="53" />

### Finding Description
`ObjectDirectoryPath` first validates that the requested object directory is *somewhere inside the storage root* (not necessarily inside the repository): [1](#0-0) 

It then computes `isTransactionQuarantineDir` purely from the final path component name: [2](#0-1) 

If that heuristic matches, the function *skips* the block that would otherwise require the path to be inside `repoPath` or to carry the repository-specific `QuarantineDirectoryPrefix(repo)` (a SHA1 hash of the repository's own relative path): [3](#0-2) [4](#0-3) 

The only remaining check is `os.Stat` for existence: [5](#0-4) 

This is confirmed by the repository's own test suite, which explicitly demonstrates that a `GitObjectDirectory` pointing at a directory named `tx-tmp/quarantine` — completely outside the target repository's own path tree and unrelated to that repository's `QuarantineDirectoryPrefix` — is accepted as a legitimate object directory: [6](#0-5) 

`GitObjectDirectory`/`GitAlternateObjectDirectories` are ordinary fields of the `Repository` message that Gitaly documents as being round-tripped through the GitLab Rails internal API during hook/quarantine handling for a push: [7](#0-6) 

The root cause mirrors the Convex bug class described in the report: the code trusts a *superficial identifying characteristic* (a pool-ID threshold in Convex; a directory basename here) as a proxy for "this is the correct/owned object," instead of verifying the actual binding between the resource and its owner. In Convex this caused wrong-address reward accounting; in Gitaly this causes wrong-directory object-store binding.

### Impact Explanation
Any caller able to set `GitObjectDirectory` on a `Repository` message for an RPC that resolves the repository's object directory (e.g. quarantine-aware operations, size/stat RPCs, or anything using `repo.ObjectDirectoryPath`/quarantine plumbing) can point that repository's effective object store at **any pre-existing directory elsewhere in the same storage whose basename is `quarantine`** (or a `tmp_objdir*` child thereof) — including another repository's or another concurrent transaction's *unrelated* quarantine directory. This breaks the isolation quarantine is meant to provide:
- Cross-repository/cross-transaction disclosure: reads against the hijacked object directory can surface objects from another user's in-flight, not-yet-accepted push.
- Potential object/state confusion for subsequent operations (size accounting, replication comparisons, RPCs like `GetObjectDirectorySize`) that trust the resolved directory belongs to the given repository, as demonstrated by the closely related "quarantined repo with different relative path" test which explicitly guards against a *different* class of cross-quarantine mixing but does not cover the basename-based bypass: [8](#0-7) 

### Likelihood Explanation
The bypass condition is trivial to satisfy: any directory literally named `quarantine` (or `tmp_objdir*` under one) that exists anywhere under the storage root qualifies, and such directories are routinely created by Gitaly itself for legitimate quarantines of *other* repositories/transactions, meaning suitable targets naturally exist during normal cluster operation (concurrent pushes). No traversal outside the storage root is needed since that is independently checked; only the "does it belong to me" check is skipped.

### Recommendation
Remove the basename-based special case, or require it to additionally satisfy the same `QuarantineDirectoryPrefix(repo)`/repository-binding check used for manual quarantine directories, so that every non-`repoPath`-relative object directory must be cryptographically tied to the specific repository it is being applied to, rather than merely matching a directory name pattern.

### Proof of Concept
1. Have two repositories/transactions active in the same storage, `A` (attacker-controlled) and `B` (victim, currently being pushed to, so Gitaly has created a quarantine dir at `<storage>/+gitaly/tmp/<txB-tmp>/quarantine` with objects for `B`'s in-flight push).
2. Issue an RPC for repository `A` with `Repository.GitObjectDirectory` set to the relative path from `A`'s repo dir to `<storage>/+gitaly/tmp/<txB-tmp>/quarantine` (or a `tmp_objdir-*` subdirectory of it).
3. `ObjectDirectoryPath` computes `baseDir == "quarantine"`, sets `isTransactionQuarantineDir = true`, and skips the ownership check that would normally require the path to belong to `A` (via `repoPath` containment or `A`'s own `QuarantineDirectoryPrefix`) — reproducing the same acceptance path exercised by the existing test case "repo quarantined by transaction manager" in `paths_test.go`, but using `B`'s real quarantine directory instead of a synthetic one.
4. Subsequent Git operations performed against repository `A` in that RPC now operate against `B`'s quarantined objects, as long as that directory exists at request time.

### Citations

**File:** internal/git/localrepo/paths.go (L32-41)
```go
	storagePath, err := repo.locator.GetStorageByName(ctx, repo.GetStorageName())
	if err != nil {
		return "", fmt.Errorf("get storage by name: %w", err)
	}

	// Ensure the path points somewhere in the storage.
	relativeObjectDirectoryPath, err := storage.ValidateRelativePath(storagePath, filepath.Join(repoPath, objectDirectoryPath))
	if err != nil {
		return "", structerr.NewInvalidArgument("validate relative path: %w", err)
	}
```

**File:** internal/git/localrepo/paths.go (L43-45)
```go
	parentDir := filepath.Base(filepath.Dir(relativeObjectDirectoryPath))
	baseDir := filepath.Base(relativeObjectDirectoryPath)
	isTransactionQuarantineDir := (baseDir == "quarantine") || ((parentDir == "quarantine") && strings.HasPrefix(baseDir, "tmp_objdir"))
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

**File:** internal/git/localrepo/paths_test.go (L81-119)
```go
	// Transactions store their set a quarantine directory in the transaction's temporary
	// directory with a path ending in `quarantine` directory. Emulate that by creating
	// such a directory in the root of the storage.
	transactionStateDir := filepath.Join(cfg.Storages[0].Path, "tx-tmp")
	transactionQuarantineDir := filepath.Join(transactionStateDir, "quarantine")
	transactionQuarantineDirWithGitPush := filepath.Join(transactionQuarantineDir, "tmp_objdir-incoming-Gbc29N")
	require.NoError(t, os.MkdirAll(transactionQuarantineDirWithGitPush, mode.Directory))
	transactionQuarantineDirRelativePath, err := filepath.Rel(repoPath, transactionQuarantineDir)
	require.NoError(t, err)
	transactionQuarantineDirWithGitPushRelativePath, err := filepath.Rel(repoPath, transactionQuarantineDirWithGitPush)
	require.NoError(t, err)

	repoWithGitObjDir := func(repo *gitalypb.Repository, dir string) *gitalypb.Repository {
		repo = proto.Clone(repo).(*gitalypb.Repository)
		repo.GitObjectDirectory = dir
		return repo
	}

	testCases := []struct {
		desc string
		repo *gitalypb.Repository
		path string
		err  codes.Code
	}{
		{
			desc: "storages configured",
			repo: repoWithGitObjDir(repoProto, "objects/"),
			path: filepath.Join(repoPath, "objects/"),
		},
		{
			desc: "repo quarantined by transaction manager",
			repo: repoWithGitObjDir(quarantinedRepo, transactionQuarantineDirRelativePath),
			path: transactionQuarantineDir,
		},
		{
			desc: "repo quarantined by transaction manager additionally quarantined by git push",
			repo: repoWithGitObjDir(quarantinedRepo, transactionQuarantineDirWithGitPushRelativePath),
			path: transactionQuarantineDirWithGitPush,
		},
```

**File:** doc/object_quarantine.md (L109-123)
```markdown
### How GitLab passes the object quarantine information around

To overcome this problem, the GitLab `pre-receive` hook
[reads the object directory configuration from its environment](https://gitlab.com/gitlab-org/gitaly/-/blob/71d527f4f16c1f0e76793f055def0299b375cc7d/internal/gitlabshell/env.go#L9).
and passes this information
[along with the HTTP API call](https://gitlab.com/gitlab-org/gitaly/-/blob/71d527f4f16c1f0e76793f055def0299b375cc7d/internal/gitaly/hook/manager.go#L30-46).
On the Rails side, we then
[put the object directory information in the "request store"](https://gitlab.com/gitlab-org/gitlab/-/blob/master/lib/api/internal/base.rb#L43)
(i.e., request-scoped thread-local storage). And then during that
Rails request, when Rails makes Gitaly requests on this repo, we send back the quarantine information
[in the Gitaly `Repository` struct](https://gitlab.com/gitlab-org/gitlab/-/blob/f81f30c29a0edce20f6737fdccc3315c8baab9d1/lib/gitlab/gitaly_client/util.rb#L8-17).
And finally, inside Gitaly, when we spawn a Git process, we
[re-create the environment variables](https://gitlab.com/gitlab-org/gitaly/-/blob/969bac80e2f246867c1a976864bd1f5b34ee43dd/internal/git/alternates/alternates.go#L21-34)
that were present on the `pre-receive` hook, so that we can see the
quarantined objects.
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
