### Title
Client-Controlled `GitAlternateObjectDirectories`/`GitObjectDirectory` Fields Bypass Transaction Isolation and Escape Path Validation - ([File: internal/gitaly/storage/storagemgr/middleware.go], [File: internal/git/gitcmd/command_factory.go])

### Summary
The `gitalypb.Repository` message exposes `git_object_directory` and `git_alternate_object_directories` fields that are normally only supposed to be populated internally, when Rails calls back into Gitaly during push access-checks against a quarantined object directory. However, these fields are ordinary, client-settable proto fields on any RPC request that carries a `Repository` message. Gitaly's request middleware explicitly detects and special-cases requests carrying these fields by treating them as "already quarantined by a previous transaction" and skipping normal transaction/snapshot management, while the actual environment-variable construction that feeds these paths into spawned `git` subprocesses does not fully validate that the alternate directories stay within the repository/storage boundary.

### Finding Description
In `beginTransactionForRepository` [1](#0-0) , any accessor RPC whose `Repository` has `GitObjectDirectory` or `GitAlternateObjectDirectories` set is treated as already coming from an authorized quarantine/snapshot flow and is routed through `restoreSnapshotRelativePath`, bypassing the transaction manager (`nonTransactionalRequest`). The code comment acknowledges this is only supposed to happen for repositories "already configured with a quarantine directory... looped back to Gitaly from Rails' authorization checks," and admits: "This property is violated in tests which manually configure the object directory... This allows for circumventing the transaction management by configuring either of the object directories. We'll leave this unaddressed for now."

Separately, when Gitaly actually spawns a `git` subprocess for a repository, `internal/git/gitcmd/command_factory.go` builds the object-directory environment directly from client-supplied fields without going through the storage-boundary validation used elsewhere: [2](#0-1) . This calls `alternates.Env`, which simply does `filepath.Join(repoPath, dir)` for every alternate directory with no escape/traversal check: [3](#0-2) .

By contrast, the one place that *does* validate `GitObjectDirectory` against the storage root and the expected quarantine-directory prefix is `Repo.ObjectDirectoryPath` [4](#0-3) , but that validation is not exercised on the direct git-command-construction path in `command_factory.go`, and it does not validate `GitAlternateObjectDirectories` entries at all — only the single `GitObjectDirectory` field.

The combination means: an ordinary API client sending any accessor RPC (e.g. an `IsAncestor`, `ListAllCommits`, `FindCommits`, or `Blobs` request, all of which take a `Repository` with these fields per the `GitAlternateObjectDirectories` usages found in their tests) can populate `git_alternate_object_directories` with a path that escapes the intended quarantine/storage sandbox (e.g. `../../other-repo/objects` or an absolute path), causing Gitaly to (1) skip normal transactional snapshot isolation for the request, and (2) spawn `git` with `GIT_ALTERNATE_OBJECT_DIRECTORIES` pointing at an unvalidated location, letting Git search that location for objects.

This is structurally analogous to the reported bug class: a mechanism meant to be reachable only from a privileged/internal caller (the Rails callback loop, conceptually similar to the MSCA calling back into itself) is instead reachable through an ordinary, unprivileged external RPC field, letting the caller invoke functionality (quarantine/transaction bypass, cross-directory object search) that should require the internal, trusted code path.

### Impact Explanation
If confirmed exploitable end-to-end, this would allow an authenticated-but-otherwise-unprivileged gRPC caller to:
- Bypass Gitaly's transaction-manager-enforced snapshot isolation for accessor RPCs on a given repository.
- Cause Git subprocesses to search alternate object directories outside the intended quarantine/storage boundary, potentially exposing objects from other repositories or arbitrary filesystem locations reachable from the Gitaly process, which is a cross-repository object access / storage escape primitive as defined in scope.

### Likelihood Explanation
The `Repository.GitObjectDirectory`/`GitAlternateObjectDirectories` fields are ordinary proto fields set directly by the request sender; no special token or internal-socket credential appears to gate their use on the external-facing accessor RPCs referenced in `internal/gitaly/service/commit/isancestor_test.go`, `internal/gitaly/service/commit/list_all_commits_test.go`, `internal/gitaly/service/blob/blobs_test.go`, and `internal/gitaly/service/commit/find_commits_test.go`, all of which manipulate these fields directly on request messages. The middleware code's own comment confirms the maintainers are aware this check can be circumvented by "manually configuring the object directory," which increases confidence this is a genuine, currently-unaddressed gap rather than a false positive.

### Recommendation
- Validate `GitAlternateObjectDirectories` (not just `GitObjectDirectory`) against the storage root and expected quarantine-directory prefix (reusing the logic in `Repo.ObjectDirectoryPath`) before it is used anywhere, including in `alternates.Env`/`command_factory.go`.
- In `beginTransactionForRepository`, do not trust client-supplied `GitObjectDirectory`/`GitAlternateObjectDirectories` as a signal that a request is legitimately already quarantined; instead, require this to be established via a trusted, unspoofable channel (e.g., a value derived server-side from the transaction ID/internal socket) rather than the raw RPC-supplied `Repository` fields.
- Reject accessor RPCs from the external-facing socket that set these fields unless the request also authenticates as coming from Gitaly's internal quarantine callback path.

### Proof of Concept
Not independently executed. Based on static analysis: send any accessor RPC to Gitaly's external gRPC listener (as an ordinary authenticated client) with `Repository.git_object_directory`/`git_alternate_object_directories` set to a path such as `../other-storage-relative-path/objects` or an absolute path outside the repository, and observe that (a) `beginTransactionForRepository` routes the request as `nonTransactionalRequest` instead of starting a normal transaction [1](#0-0) , and (b) the spawned `git` process receives a `GIT_ALTERNATE_OBJECT_DIRECTORIES` environment variable built via unchecked `filepath.Join` [3](#0-2) .

**Uncertainty:** I could not confirm within the available tool budget whether `internal/gitaly/config/locator.go`'s `ValidateRepository` implementation (referenced but not read) independently rejects malformed `GitAlternateObjectDirectories` before requests reach `command_factory.go`/`middleware.go`. If that function does perform equivalent boundary validation for both fields, the practical exploitability of this analog would be reduced or eliminated. This should be verified directly against `internal/gitaly/config/locator.go` before treating this as confirmed.

### Citations

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

**File:** internal/git/localrepo/paths.go (L19-83)
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

	fullPath := filepath.Join(repoPath, objectDirectoryPath)
	if _, err := os.Stat(fullPath); os.IsNotExist(err) {
		return "", structerr.NewNotFound("object directory does not exist: %q", fullPath)
	}

	return fullPath, nil
}
```
