### Title
Unvalidated `GitObjectDirectory`/`GitAlternateObjectDirectories` fields allow object-directory path traversal in git command construction - ([File: internal/git/gitcmd/command_factory.go])

### Summary
`ValidateRepository`/`GetRepoPath` in `internal/gitaly/config/locator.go` only validate `StorageName` and `RelativePath` against path-escape via `storage.ValidateRelativePath`, but never validate the `GitObjectDirectory` / `GitAlternateObjectDirectories` fields of the `Repository` proto message. [1](#0-0) 
Yet these same unvalidated fields are read directly by `ExecCommandFactory.newCommand()` and turned into `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES` environment variables for every spawned git process, via `alternates.Env(repoPath, repo.GetGitObjectDirectory(), repo.GetGitAlternateObjectDirectories())`. [2](#0-1) [3](#0-2) 

This mirrors the ERC2981 bug class: there are two logically-linked pieces of state describing the effective object storage location for a repository — the field that is *actually used* to drive git process execution (`GitObjectDirectory`/`GitAlternateObjectDirectories`, analogous to `works[tokenId].strategy.royaltyBps` used in payments) and the validated/derived path resolution performed elsewhere (`Repo.ObjectDirectoryPath()` in `internal/git/localrepo/paths.go`, which does enforce quarantine-prefix / root-containment checks, analogous to `_setTokenRoyalty()`). The setter/consumer of the authoritative value (`newCommand`) never invokes the validating function, so the two representations of "the object directory for this git invocation" go out of sync — one path is checked, the other, actually-used path is not.

### Finding Description
`Repo.ObjectDirectoryPath()` performs careful validation: it checks whether the relative object directory is contained within the repository, or otherwise whether it matches an expected quarantine-directory prefix under the storage's temp directory, rejecting anything else as invalid. [4](#0-3) 

However, this validation is not invoked from the actual command-execution path. `ExecCommandFactory.newCommand()` builds the environment for every git subprocess by calling `alternates.Env(repoPath, repo.GetGitObjectDirectory(), repo.GetGitAlternateObjectDirectories())` directly on the raw fields of the `*gitalypb.Repository` supplied with the RPC request, with no call to `ObjectDirectoryPath()`, `ValidateRelativePath()`, or any other containment check. [2](#0-1) 

`alternates.Env()` blindly `filepath.Join`s the (attacker-influenced) `objectDirectory`/`alternateObjectDirectories` values onto `repoPath` without any bounds checking, so a value such as `../../other-storage/other-repo/objects` or an absolute path would be joined and exported as `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES` verbatim. [3](#0-2) 

Because `configLocator.ValidateRepository()` (invoked by `GetRepoPath()`, which *is* called in `newCommand`) only validates `StorageName`/`RelativePath`, it never inspects `GitObjectDirectory`/`GitAlternateObjectDirectories` at all before the git process is spawned with those values injected as environment variables. [5](#0-4) 

The result is a state-consistency gap identical in shape to the ERC2981 report: the "trusted" quarantine/object-directory validation logic exists (`ObjectDirectoryPath`), but the actual state (`GitObjectDirectory` field) that drives real behavior (spawning git with those env vars) bypasses it entirely on the primary git-command-construction path.

### Impact Explanation
If any RPC accepts a `Repository` message with a caller-controlled `git_object_directory` / `git_alternate_object_directories` (these are legitimate, documented protobuf fields intended for the Rails-driven object-quarantine feature; see `proto/shared.proto` field comments), a crafted value can point `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES` outside of the intended repository or even outside the intended storage root when git commands are executed for that request. This can result in cross-repository object exposure/injection (git commands searching for or writing objects in an attacker-chosen directory) — a storage-boundary escape enabled purely by the fact that the environment-construction path never reuses the validation logic implemented for the equivalent path-resolution helper. [6](#0-5) 

### Likelihood Explanation
The `GitObjectDirectory`/`GitAlternateObjectDirectories` fields are part of the standard `gitalypb.Repository` message that is threaded through virtually all RPCs and is explicitly designed to be set by the caller (GitLab Rails) to point Gitaly at a quarantine directory for a given push, per the documented quarantine flow. [7](#0-6) 
Any code path that constructs a `*gitalypb.Repository` with these fields populated from external/relative-path input and passes it to `newCommand` (i.e., essentially every git command invocation) is affected, since the validation gap is in the shared `ExecCommandFactory.newCommand()`/`configLocator` code used by nearly all RPC handlers. I was not able to fully confirm within the available context whether every ingress point additionally sanitizes these fields before they reach `newCommand` (e.g., some request-specific validators might reject unexpected object-directory values), so likelihood should be considered "plausible but unconfirmed end-to-end" pending a check of all RPC-level request validators that populate/forward these two fields.

### Recommendation
Before `ExecCommandFactory.newCommand()` (or `alternates.Env`) uses `repo.GetGitObjectDirectory()`/`repo.GetGitAlternateObjectDirectories()` to build the git process environment, route both fields through the same containment/quarantine-prefix validation already implemented in `Repo.ObjectDirectoryPath()` (or an equivalent shared helper), rejecting any repository whose object-directory fields resolve outside of the repository root or the expected quarantine-directory prefix. Equivalently, harden `configLocator.ValidateRepository()`/`GetRepoPath()` to validate `GitObjectDirectory` and each entry of `GitAlternateObjectDirectories` via `storage.ValidateRelativePath` (or the quarantine-prefix check) so that no caller can ever get an unvalidated value into `newCommand`.

### Proof of Concept
Conceptual PoC (not executed, based on code-path analysis):
1. An RPC handler builds a `*gitalypb.Repository{StorageName: "default", RelativePath: "repo.git", GitObjectDirectory: "../../other-repo/objects"}` from caller-supplied/quarantine-passthrough data.
2. This repository proto is passed to any RPC that spawns a git command against it (e.g. via `localrepo.Repo` methods backed by `ExecCommandFactory`).
3. `configLocator.ValidateRepository()`/`GetRepoPath()` validates only `StorageName`/`RelativePath` and returns `repoPath` successfully; `GitObjectDirectory` is never checked. [8](#0-7) 
4. `newCommand()` calls `alternates.Env(repoPath, "../../other-repo/objects", nil)`, which does `filepath.Join(repoPath, "../../other-repo/objects")`, producing a path outside `repoPath`, and sets `GIT_OBJECT_DIRECTORY` to that path for the spawned git process. [2](#0-1) [9](#0-8) 
5. Contrast this with `Repo.ObjectDirectoryPath()`, which would reject the same value as escaping the repository (unless it matched the quarantine-prefix pattern), demonstrating the two code paths are inconsistent for the same underlying field. [10](#0-9) 

**Note on completeness:** Due to index size limits, I could not exhaustively trace every RPC entry point that constructs the `Repository` proto with these fields to confirm which are reachable from an unprivileged/ordinary user versus only from the trusted GitLab-Rails-to-Gitaly quarantine hand-off. A full Devin session with complete repository access would be needed to enumerate all reachable ingress points and confirm exploitability end-to-end.

### Citations

**File:** internal/gitaly/config/locator.go (L47-149)
```go
func (l *configLocator) ValidateRepository(ctx context.Context, repo storage.Repository, opts ...storage.ValidateRepositoryOption) error {
	var cfg storage.ValidateRepositoryConfig
	for _, opt := range opts {
		opt(&cfg)
	}

	// Only checking for `nil` isn't sufficient as Protobuf messages may be non-nil, but still
	// either invalid or empty. Thus we also explicitly verify whether both the storage name and
	// the relative path are unset.
	if repo == nil || repo.GetStorageName() == "" && repo.GetRelativePath() == "" {
		return structerr.NewInvalidArgument("%w", storage.ErrRepositoryNotSet)
	}

	relativePath := repo.GetRelativePath()
	if len(relativePath) == 0 {
		return structerr.NewInvalidArgument("%w", storage.ErrRepositoryPathNotSet)
	}

	if cfg.SkipStorageExistenceCheck {
		return nil
	}

	storagePath, err := l.GetStorageByName(ctx, repo.GetStorageName())
	if err != nil {
		return err
	}

	if _, err := os.Stat(storagePath); err != nil {
		if os.IsNotExist(err) {
			return structerr.NewNotFound("storage does not exist").WithMetadata("storage_path", storagePath)
		}
		return structerr.New("storage path: %w", err).WithMetadata("storage_path", storagePath)
	}

	if _, err := storage.ValidateRelativePath(storagePath, relativePath); err != nil {
		return structerr.NewInvalidArgument("%w", err).WithMetadata("relative_path", relativePath)
	}

	path := filepath.Join(storagePath, repo.GetRelativePath())
	if path == "" {
		return structerr.NewInvalidArgument("repository path is empty")
	}

	if !cfg.SkipRepositoryExistenceCheck {
		if err := storage.ValidateGitDirectory(path); err != nil {
			if errors.Is(err, os.ErrNotExist) {
				return storage.NewRepositoryNotFoundError(repo.GetStorageName(), repo.GetRelativePath())
			}

			var errInvalidGitDir storage.InvalidGitDirectoryError
			if errors.As(err, &errInvalidGitDir) {
				return structerr.NewFailedPrecondition(
					"%w: %q does not exist", storage.ErrRepositoryNotValid, errInvalidGitDir.MissingEntry,
				).WithMetadata("repository_path", path)
			}

			return structerr.New("validate git directory: %w", err).WithMetadata("repository_path", path)
		}

		// See: https://gitlab.com/gitlab-org/gitaly/issues/1339
		//
		// This is a workaround for Gitaly running on top of an NFS mount. There
		// is a Linux NFS v4.0 client bug where opening the packed-refs file can
		// either result in a stale file handle or stale data. This can happen if
		// git gc runs for a long time while keeping open the packed-refs file.
		// Running stat() on the file causes the kernel to revalidate the cached
		// directory entry. We don't actually care if this file exists.
		_, _ = os.Stat(filepath.Join(path, "packed-refs"))
	}

	return nil
}

// GetRepoPath returns the full path of the repository referenced by an RPC Repository message.
// By default, it verifies that the path is an existing git directory. However, if invoked with
// the `GetRepoPathOption` produced by `WithRepositoryVerificationSkipped()`, this validation
// will be skipped. The errors returned are gRPC errors with relevant error codes and should be
// passed back to gRPC without further decoration.
func (l *configLocator) GetRepoPath(ctx context.Context, repo storage.Repository, opts ...storage.GetRepoPathOption) (string, error) {
	var cfg storage.GetRepoPathConfig
	for _, opt := range opts {
		opt(&cfg)
	}

	var validationOptions []storage.ValidateRepositoryOption
	if cfg.SkipRepositoryVerification {
		validationOptions = []storage.ValidateRepositoryOption{
			storage.WithSkipRepositoryExistenceCheck(),
		}
	}

	if err := l.ValidateRepository(ctx, repo, validationOptions...); err != nil {
		return "", err
	}

	storagePath, err := l.GetStorageByName(ctx, repo.GetStorageName())
	if err != nil {
		return "", err
	}
	relativePath := repo.GetRelativePath()

	return filepath.Join(storagePath, relativePath), nil
}
```

**File:** internal/git/gitcmd/command_factory.go (L511-519)
```go
	var repoPath string
	if repo != nil {
		var err error
		repoPath, err = cf.locator.GetRepoPath(ctx, repo)
		if err != nil {
			return nil, err
		}

		env = append(alternates.Env(repoPath, repo.GetGitObjectDirectory(), repo.GetGitAlternateObjectDirectories()), env...)
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

**File:** proto/shared.proto (L58-64)
```text
  // git_object_directory sets the GIT_OBJECT_DIRECTORY envvar on git commands to the value of this field.
  // It influences the object storage directory the SHA1 directories are created underneath.
  string git_object_directory = 4;
  // git_alternate_object_directories sets the GIT_ALTERNATE_OBJECT_DIRECTORIES envvar on git commands to
  // the values of this field. It influences the list of Git object directories which can be used to search
  // for Git objects.
  repeated string git_alternate_object_directories = 5;
```

**File:** doc/object_quarantine.md (L109-124)
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
