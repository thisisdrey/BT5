Based on my investigation, the strongest analog to the H-01 bug class exists in how Gitaly resolves the Git object-directory / alternates fields when spawning git subprocesses.

### Title
Git object-directory/alternates fields from the `Repository` proto bypass path validation when git commands are spawned - (File: internal/git/gitcmd/command_factory.go)

### Summary
The H-01 report describes a class of bug where a security restriction is enforced on one code path (`FighterFarm.transferFrom`/`safeTransferFrom`) but a second, inherited/lower-level primitive (`ERC721.safeTransferFrom(..., data)`) that performs the same underlying operation is reachable directly and skips the restriction. Gitaly has a structurally similar duplication: there is a *validated* accessor for a repository's object directory, `Repo.ObjectDirectoryPath()`, but the actual hot path that constructs every git subprocess invocation does not use it — it consumes the raw `Repository.GitObjectDirectory` / `Repository.GitAlternateObjectDirectories` proto fields directly.

### Finding Description
`internal/git/localrepo/paths.go`'s `ObjectDirectoryPath()` carefully validates that the repository's configured git object directory is either inside the repository/storage tree, or matches the specific quarantine-directory prefix that Gitaly itself creates: [1](#0-0) [2](#0-1) 

However, this validated function is not what is used when git subprocesses are actually spawned. `ExecCommandFactory.newCommand()`, which backs `CommandFactory.New()` used by essentially every RPC handler to run a git command against a repository, builds the `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES` environment variables straight from the unvalidated proto getters: [3](#0-2) 

`repo.GetGitObjectDirectory()` and `repo.GetGitAlternateObjectDirectories()` are plain getters on the `gitalypb.Repository` message fields, defined in the `storage.Repository` interface with no path validation attached: [4](#0-3) 

These fields exist specifically to let Rails point Gitaly at the temporary object-quarantine directory created during `pre-receive` (as documented), and the intended safety net is exactly the check performed in `ObjectDirectoryPath()` — that the directory must be inside the repo or must match the quarantine-prefix scheme: [5](#0-4) 

But `newCommand()` never calls `ObjectDirectoryPath()` (or any equivalent `storage.ValidateRelativePath`/quarantine-prefix check) before turning these fields into `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES` environment variables for the spawned git process. The validation only exists in a separate accessor that appears to be used elsewhere (e.g., for existence checks), not in the command-construction path that actually matters.

### Impact Explanation
Since virtually all git subcommands executed by Gitaly for a repository go through `ExecCommandFactory.newCommand()`, any RPC whose request contains a `gitalypb.Repository` message with attacker/caller-controlled `git_object_directory` or `git_alternate_object_directories` fields can cause the spawned `git` process to search for (and in some commands, write) objects in an arbitrary filesystem path outside of the target repository and outside of any legitimate quarantine directory — the equivalent of the "storage escape" / "cross-repository object access" categories called out in scope. This could let an ordinary caller make Gitaly read objects from another repository's object store (cross-repository object disclosure) or point Git at arbitrary directories via a crafted RPC field, since the only enforcement point (`ObjectDirectoryPath`) is not consulted on this path.

### Likelihood Explanation
This is directly analogous to the H-01 pattern: a "belt" validation function exists (`ObjectDirectoryPath`), but the actual privileged operation (spawning a git process with quarantine/alternates env vars) is performed through a different, lower-level code path (`newCommand`/`alternates.Env`) that omits the check — exactly like `_ableToTransfer()` guarding `transferFrom`/`safeTransferFrom` while the inherited `safeTransferFrom(..., data)` bypassed it. Reachability is broad because `newCommand()` executes for essentially every RPC that runs a git subcommand against a repository, and the `git_object_directory`/`git_alternate_object_directories` fields are ordinary fields of the `Repository` message included in RPC requests.

### Recommendation
Route the object-directory and alternate-object-directory values used in `ExecCommandFactory.newCommand()` through the same validation that `Repo.ObjectDirectoryPath()` performs (i.e., call `storage.ValidateRelativePath`/quarantine-prefix checks) before constructing `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES`, rather than consuming the raw proto getters directly. Centralize this validation in a single function used by all code paths that translate these fields into git environment variables, analogous to moving the AI Arena fix into the `_beforeTokenTransfer()` hook so no alternate entry point can bypass it.

### Proof of Concept
I was not able to fully inspect `internal/git/alternates/alternates.go` (the `Env()` function body) within the available tool budget, so I cannot show the exact final environment-variable string construction or confirm whether any additional sanitization happens inside `alternates.Env()` itself. This is a real gap in my verification: the finding is based on tracing `newCommand()`'s call site and the absence of any call to `ObjectDirectoryPath()`/`ValidateRelativePath` in that path, cross-referenced against the validation that clearly exists elsewhere (`paths.go`) and the documented purpose of these fields (`doc/object_quarantine.md`). Confirming exploitability end-to-end (e.g., building a request with `git_alternate_object_directories` set to `../../other-repo/objects` and observing cross-repo object access) requires running Gitaly and issuing a crafted RPC, which is outside the scope of static code inspection here.

### Citations

**File:** internal/git/localrepo/paths.go (L19-41)
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

**File:** internal/gitaly/storage/locator.go (L61-69)
```go
// Repository represents a storage-scoped repository.
type Repository interface {
	GetStorageName() string
	GetRelativePath() string
	GetGitObjectDirectory() string
	GetGitAlternateObjectDirectories() []string
	GetGlRepository() string
	GetGlProjectPath() string
}
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
