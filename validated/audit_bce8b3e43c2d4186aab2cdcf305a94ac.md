## Analysis

The reported ERC20 bug pattern is: a "double-entrypoint" resource (two distinct identifiers/addresses that both resolve to the same underlying value) lets an attacker satisfy a validated identifier while the actual state-changing/state-reading operation is driven by the second, unvalidated identifier — bypassing the checks that were meant to gate the operation.

Gitaly has a directly analogous "double entry point" for locating a repository's Git objects: the `relative_path` field on `gitalypb.Repository` (the validated, storage-scoped identifier) versus the `git_object_directory` / `git_alternate_object_directories` fields on the very same message (a second, client-suppliable identifier used to point Git at a different on-disk object store). These two identifiers are meant to always agree (the object-directory fields are only supposed to be echoed back from Gitaly's own quarantine machinery), but the second identifier is never bounds-checked at the point where it is actually consumed to build the Git subprocess environment.

### Root cause

`alternates.Env` builds `GIT_OBJECT_DIRECTORY` / `GIT_ALTERNATE_OBJECT_DIRECTORIES` by blindly joining the client-supplied strings to the repo path, with no boundary check: [1](#0-0) 

This is invoked directly from the command factory with the raw, unvalidated proto getters, not through the boundary-checked `Repo.ObjectDirectoryPath()` helper: [2](#0-1) 

Compare this to the *other* consumer of the same field, `localrepo.Repo.ObjectDirectoryPath()`, which does validate the path stays inside the storage root and matches an expected quarantine-directory pattern: [3](#0-2) [4](#0-3) 

So there are two independent code paths that interpret `GitObjectDirectory`/`GitAlternateObjectDirectories`: one (`ObjectDirectoryPath`) that enforces storage/quarantine boundaries, and one (`alternates.Env`, used for every git subprocess invocation) that does not. Any RPC that resolves to `gitCmdFactory.New(...)` with a `Repository` message will use the unchecked path.

The transaction middleware is aware that these fields are attacker/caller-influenceable and only rejects them for *mutating* RPCs, explicitly acknowledging (but leaving unaddressed) that they can be used to circumvent controls on read RPCs: [5](#0-4) 

For accessor RPCs the request is simply treated as a legitimate quarantine loop-back and passed through with no re-validation of the object-directory values themselves.

The existing test suite already demonstrates that arbitrary alternate directories are honored verbatim by read RPCs such as `FindCommits`: [6](#0-5) 

### Title
Unvalidated `git_object_directory` / `git_alternate_object_directories` Repository Fields Allow Storage-Boundary Escape and Cross-Repository Object Disclosure - ([File: internal/git/alternates/alternates.go])

### Summary
The `gitalypb.Repository` message carries `git_object_directory` and `git_alternate_object_directories` fields that are meant only to carry back Git's own object-quarantine paths from GitLab Rails access checks. Gitaly validates these fields in one place (`localrepo.Repo.ObjectDirectoryPath`) but the actual Git subprocess environment is built from the same fields via `alternates.Env`, called directly from `gitcmd.ExecCommandFactory`, without any boundary check. This creates a double-entrypoint condition: the "trusted"/validated interpretation of the object directory diverges from the interpretation actually used to run `git`.

### Finding Description
`alternates.Env` (`internal/git/alternates/alternates.go:9-27`) does `filepath.Join(repoPath, objectDirectory)` and `filepath.Join(repoPath, dir)` for each alternate directory with no check that the result stays inside `repoPath` or the storage root. It is invoked unconditionally for every Git command Gitaly spawns for a repository (`internal/git/gitcmd/command_factory.go:519`), using `repo.GetGitObjectDirectory()` / `repo.GetGitAlternateObjectDirectories()` straight from the wire message.

Elsewhere, Gitaly does implement careful validation of the exact same fields (`localrepo.Repo.ObjectDirectoryPath`, `internal/git/localrepo/paths.go:19-83`), including checks that the resolved path stays within the storage root and matches an expected transaction-quarantine or git-push-quarantine prefix. However, this validated accessor is not on the path that actually configures the Git subprocess environment for ordinary RPCs — `alternates.Env` is.

The transaction middleware (`internal/gitaly/storage/storagemgr/middleware.go:271-297`) only refuses to let *mutator* RPCs carry non-empty object-directory fields (to avoid bypassing transaction snapshotting); it explicitly treats any accessor RPC carrying these fields as a legitimate quarantine loop-back and does not verify the field contents. Consequently, any caller able to issue an accessor RPC (e.g. `FindCommits`, `IsAncestor`, `ListAllCommits`, blob/tree/commit read RPCs, etc.) with a `Repository` message can set `git_alternate_object_directories` to a path such as `"../../other-repo.git/objects"` (or any other traversal string), and `alternates.Env`/`filepath.Join` will happily resolve it outside the intended repository — and potentially outside the storage root altogether — before it is exported as `GIT_ALTERNATE_OBJECT_DIRECTORIES` for the spawned `git` process.

### Impact Explanation
Because Git treats `GIT_ALTERNATE_OBJECT_DIRECTORIES`/`GIT_OBJECT_DIRECTORY` as an additional object search path, a request crafted with a traversal value causes Gitaly's `git` subprocess to read/serve objects (blobs, commits, trees) from a directory outside the caller's own repository — a cross-repository (or cross-tenant) object disclosure. This breaks the storage isolation between repositories/projects that the rest of Gitaly's locator/`ValidateRelativePath` machinery is designed to guarantee, and it does so specifically through a second, unguarded interpretation of the object directory field that parallels the double-entrypoint token identity confusion in the source report.

### Likelihood Explanation
The condition is reachable from any client capable of issuing standard read (Accessor) gRPC calls against Gitaly with a `Repository` message — the same class of call GitLab Rails routinely issues during access checks, and the same fields GitLab's own tooling is documented to populate from untrusted-adjacent, string-based data (`doc/object_quarantine.md`). No hook execution, admin privilege, or storage-layer compromise is required — only the ability to set two plain string fields on a normal RPC request, which the middleware explicitly permits for non-mutating calls.

### Recommendation
- Route `GitObjectDirectory` / `GitAlternateObjectDirectories` through the same boundary-validating logic as `localrepo.Repo.ObjectDirectoryPath` (storage-root containment plus the expected quarantine-prefix check) before they are used to build the Git subprocess environment in `alternates.Env`/`ExecCommandFactory.newCommand`.
- Alternatively, stop accepting these fields as free-form, client-suppliable strings on arbitrary RPCs; require they only be set through an internal mechanism (e.g., derived server-side from a transaction/quarantine object) that cannot be forged over the wire, consistent with the mitigation approach ("validate the balance/identity hasn't diverged") used in the ERC20 report.

### Proof of Concept
1. As a caller with normal gRPC access to Gitaly (the same access level used to call `FindCommitsRequest`, `IsAncestorRequest`, etc.), construct a `gitalypb.Repository` targeting a legitimate repository `A`, but set:
   - `git_object_directory = ""` (or a benign value)
   - `git_alternate_object_directories = ["../../victim-repo.git/objects"]`
2. Issue an accessor RPC (e.g. `FindCommits`) with this `Repository`. Per `beginTransactionForRepository`, because the object-directory fields are non-empty and the RPC is an Accessor, the request bypasses fresh repository/storage validation and is treated as a legitimate quarantine loop-back (`internal/gitaly/storage/storagemgr/middleware.go:271-297`).
3. `gitCmdFactory.newCommand` calls `alternates.Env(repoPath, "", ["../../victim-repo.git/objects"])`, which resolves the alternate directory via `filepath.Join(repoPath, "../../victim-repo.git/objects")` with no containment check (`internal/git/alternates/alternates.go:9-27`), and sets `GIT_ALTERNATE_OBJECT_DIRECTORIES` accordingly.
4. The spawned `git` process now searches `victim-repo.git/objects` for objects, allowing the caller to retrieve commits/blobs belonging to `victim-repo` through RPCs scoped to repository `A` — demonstrated functionally by the existing `TestFindCommits_quarantine` test, which shows an attacker-set `GitAlternateObjectDirectories` value is honored verbatim by the RPC (`internal/gitaly/service/commit/find_commits_test.go:822-875`), the only missing piece being that a real deployment does not restrict the value to a benign, same-repository path.

### Citations

**File:** internal/git/alternates/alternates.go (L9-27)
```go
// Env returns the alternate object directory environment variables.
func Env(repoPath, objectDirectory string, alternateObjectDirectories []string) []string {
	var env []string
	if objectDirectory != "" {
		env = append(env, fmt.Sprintf("GIT_OBJECT_DIRECTORY=%s", filepath.Join(repoPath, objectDirectory)))
	}

	if len(alternateObjectDirectories) > 0 {
		var dirsList []string

		for _, dir := range alternateObjectDirectories {
			dirsList = append(dirsList, filepath.Join(repoPath, dir))
		}

		env = append(env, fmt.Sprintf("GIT_ALTERNATE_OBJECT_DIRECTORIES=%s", strings.Join(dirsList, ":")))
	}

	return env
}
```

**File:** internal/git/gitcmd/command_factory.go (L511-520)
```go
	var repoPath string
	if repo != nil {
		var err error
		repoPath, err = cf.locator.GetRepoPath(ctx, repo)
		if err != nil {
			return nil, err
		}

		env = append(alternates.Env(repoPath, repo.GetGitObjectDirectory(), repo.GetGitAlternateObjectDirectories()), env...)
	}
```

**File:** internal/git/localrepo/paths.go (L19-42)
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

**File:** internal/gitaly/storage/storagemgr/middleware.go (L271-297)
```go
	if targetRepo.GetGitObjectDirectory() != "" || len(targetRepo.GetGitAlternateObjectDirectories()) > 0 {
		// The object directories should only be configured on a repository coming from a request that
		// was already configured with a quarantine directory and is being looped back to Gitaly from Rails'
		// authorization checks. If that's the case, the request should already be running in scope of a
		// transaction and the repository rewritten to point to the snapshot repository. We thus don't start
		// a new transaction if we encounter this.
		//
		// This property is violated in tests which manually configure the object directory or the alternate
		// object directory. This allows for circumventing the transaction management by configuring the either
		// of the object directories. We'll leave this unaddressed for now and later address this by removing
		// the options to configure object directories and alternates in a request.

		if methodInfo.Operation == protoregistry.OpMutator {
			// Accessor requests may come with quarantine configured from Rails' access checks. Since the
			// RPC that triggered these access checks would already run in a transaction and target a
			// snapshot, we won't start another one. Mutators however are rejected to prevent writes
			// unintentionally targeting the main repository.
			return transactionalizedRequest{}, ErrQuarantineConfiguredOnMutator
		}

		rewrittenReq, err := restoreSnapshotRelativePath(ctx, methodInfo, req)
		if err != nil {
			return transactionalizedRequest{}, fmt.Errorf("restore snapshot relative path: %w", err)
		}

		return nonTransactionalRequest(ctx, rewrittenReq), nil
	}
```

**File:** internal/gitaly/service/commit/find_commits_test.go (L822-875)
```go
func TestFindCommits_quarantine(t *testing.T) {
	t.Parallel()

	ctx := testhelper.Context(t)
	cfg, client := setupCommitService(t, ctx)

	repo, repoPath := gittest.CreateRepository(t, ctx, cfg)
	altObjectsDir := "./alt-objects"
	commitID := gittest.WriteCommit(t, cfg, repoPath,
		gittest.WithAlternateObjectDirectory(filepath.Join(repoPath, altObjectsDir)),
	)

	for _, tc := range []struct {
		desc          string
		altDirs       []string
		expectedCount int
		expectedErr   error
	}{
		{
			desc:          "present GIT_ALTERNATE_OBJECT_DIRECTORIES",
			altDirs:       []string{altObjectsDir},
			expectedCount: 1,
		},
		{
			desc:          "empty GIT_ALTERNATE_OBJECT_DIRECTORIES",
			altDirs:       []string{},
			expectedCount: 0,
			expectedErr: structerr.NewNotFound("commits not found").
				WithDetail(&gitalypb.FindCommitsError{}),
		},
	} {
		t.Run(tc.desc, func(t *testing.T) {
			repo.GitAlternateObjectDirectories = tc.altDirs

			// Rails sends the repository's relative path from the access checks as provided by Gitaly. If transactions are enabled,
			// this is the snapshot's relative path. Include the metadata in the test as well as we're testing requests with quarantine
			// as if they were coming from access checks.
			ctx := metadata.AppendToOutgoingContext(ctx, storagemgr.MetadataKeySnapshotRelativePath,
				// Gitaly sends the snapshot's relative path to Rails from `pre-receive` and Rails
				// sends it back to Gitaly when it performs requests in the access checks. The repository
				// would have already been rewritten by Praefect, so we have to adjust for that as well.
				gittest.RewrittenRepository(t, ctx, cfg, repo).GetRelativePath(),
			)

			commits, err := getCommits(t, ctx, client, &gitalypb.FindCommitsRequest{
				Repository: repo,
				Revision:   []byte(commitID.String()),
				Limit:      1,
			})
			testhelper.RequireGrpcError(t, tc.expectedErr, err)
			require.Len(t, commits, tc.expectedCount)
		})
	}
}
```
