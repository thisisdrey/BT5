## Title
Unvalidated caller-supplied `GitObjectDirectory`/`GitAlternateObjectDirectories` fields are trusted directly when constructing Git process environment, bypassing storage-boundary checks - (File: internal/git/gitcmd/command_factory.go)

### Summary
The `Repository` protobuf message accepted on essentially every Gitaly RPC includes two caller-controlled fields, `git_object_directory` and `git_alternate_object_directories`, which are documented to set `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES` on spawned Git processes. [1](#0-0)  Unlike the Sherlock report's `IControlTower` case where a security decision trusted a caller-supplied authority object instead of the contract's own trusted `controlTower` state, here Gitaly's command-construction path trusts these caller-supplied directory fields directly instead of routing them through the locator's own trusted, storage-boundary-validating logic.

### Finding Description
When Gitaly builds a `git` command via `ExecCommandFactory.newCommand`, it takes the repository path from the (validated) locator, but then injects the *unvalidated* caller-supplied object-directory fields straight into the process environment:

```go
env = append(alternates.Env(repoPath, repo.GetGitObjectDirectory(), repo.GetGitAlternateObjectDirectories()), env...)
``` [2](#0-1) 

`alternates.Env` performs no boundary checking whatsoever — it simply `filepath.Join`s the repo path with whatever value the caller provided:

```go
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
``` [3](#0-2) 

This is in stark contrast to the code path Gitaly *does* trust for this purpose: `Repo.ObjectDirectoryPath()`, which validates that the object directory either stays within the repository's own storage (`storage.ValidateRelativePath(storagePath, ...)`) or matches an expected quarantine-directory prefix computed server-side. [4](#0-3)  That validation, however, is only invoked by a small number of explicit call sites such as `GetObjectDirectorySize` — it is not enforced by the generic Git command factory that essentially every RPC uses to spawn Git subprocesses. [5](#0-4) 

The generic `locator.ValidateRepository()` invoked by RPC handlers before building commands only checks `storage_name`/`relative_path`, never `git_object_directory` or `git_alternate_object_directories`:

```go
func (l *configLocator) ValidateRepository(ctx context.Context, repo storage.Repository, ...) error {
    ...
    relativePath := repo.GetRelativePath()
    ...
    if _, err := storage.ValidateRelativePath(storagePath, relativePath); err != nil {
``` [6](#0-5) 

The intended, trusted mechanism for these fields is the quarantine round-trip documented in `doc/object_quarantine.md`: Gitaly itself computes a quarantine directory server-side, and Rails is only supposed to echo that same value back during access-check callbacks. [7](#0-6)  But because the Git command factory accepts and directly consumes whatever value arrives in the `Repository` message field — without confirming it matches the server-issued quarantine path or otherwise stays within the storage root — any RPC caller able to set `git_object_directory`/`git_alternate_object_directories` on a request can steer `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES` outside of the intended repository, e.g. via `../../` traversal sequences that `filepath.Join` will happily resolve upward and out of `repoPath`, since no post-join boundary check exists in this path (unlike `ObjectDirectoryPath`, `storagemgr` middleware only special-cases these fields for *mutator* rejection/transaction bypass detection, not for validating their content against storage boundaries). [8](#0-7) 

### Impact Explanation
A crafted `git_alternate_object_directories`/`git_object_directory` value that escapes the intended repository can cause Git subprocesses to read objects from — or, for the main object directory, write new objects into — an arbitrary path reachable from the storage root, including other repositories' object stores. This is a cross-repository object-access / storage-boundary escape: an attacker-influenced accessor request could pull objects that belong to a different repository/tenant into command output, or a mutator-adjacent flow could corrupt another repository's object database, undermining Gitaly's per-repository isolation guarantees.

### Likelihood Explanation
Reachability requires only that a caller can populate the `git_object_directory`/`git_alternate_object_directories` fields on a `Repository` message sent to an RPC that subsequently executes a Git command — these fields are ordinary, client-settable protobuf fields present on essentially all Gitaly requests, not privileged/internal-only fields, and the documented quarantine round-trip via Rails is exactly the mechanism that legitimately sets them, showing this is a normally-exercised path rather than a hypothetical one. [7](#0-6)  The only place doing rigorous validation (`ObjectDirectoryPath`) is not on the generic command-construction path, so the gap is real and not superficial.

### Recommendation
Route `GitObjectDirectory`/`GitAlternateObjectDirectories` through the same validation used by `Repo.ObjectDirectoryPath()` (storage-root containment plus quarantine-prefix verification) before they are consumed by `alternates.Env` in `ExecCommandFactory.newCommand`, rather than trusting the raw caller-supplied values for environment construction.

### Proof of Concept
Not applicable/available from static review; would require constructing a request whose `Repository.git_alternate_object_directories` contains a `..`-traversal sequence and observing the resulting `GIT_ALTERNATE_OBJECT_DIRECTORIES` env var and consequent object visibility across the storage boundary via a Devin session with full repo/test access.

### Citations

**File:** proto/go/gitalypb/shared.pb.go (L253-259)
```go
	// git_object_directory sets the GIT_OBJECT_DIRECTORY envvar on git commands to the value of this field.
	// It influences the object storage directory the SHA1 directories are created underneath.
	GitObjectDirectory string `protobuf:"bytes,4,opt,name=git_object_directory,json=gitObjectDirectory,proto3" json:"git_object_directory,omitempty"`
	// git_alternate_object_directories sets the GIT_ALTERNATE_OBJECT_DIRECTORIES envvar on git commands to
	// the values of this field. It influences the list of Git object directories which can be used to search
	// for Git objects.
	GitAlternateObjectDirectories []string `protobuf:"bytes,5,rep,name=git_alternate_object_directories,json=gitAlternateObjectDirectories,proto3" json:"git_alternate_object_directories,omitempty"`
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

**File:** internal/gitaly/service/repository/size_test.go (L312-317)
```go
		response, err := client.GetObjectDirectorySize(ctx, &gitalypb.GetObjectDirectorySizeRequest{
			Repository: repo,
		})
		require.Error(t, err, "rpc error: code = InvalidArgument desc = GetObjectDirectoryPath: relative path escapes root directory")
		require.Nil(t, response)
	})
```

**File:** internal/gitaly/config/locator.go (L47-83)
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
```

**File:** doc/object_quarantine.md (L109-120)
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
