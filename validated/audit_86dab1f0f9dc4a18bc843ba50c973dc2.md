### Title
Unsanitized `GitObjectDirectory`/`GitAlternateObjectDirectories` fields on the `Repository` proto allow any RPC caller to redirect Git's object lookup outside the repository - ([File: internal/git/alternates/alternates.go])

### Summary
The `gitalypb.Repository` message exposes `git_object_directory` and `git_alternate_object_directories` fields that any gRPC client can set on virtually any RPC request. These fields are meant only to carry quarantine-directory information for the internal pre-receive/access-check loop-back flow, but they are consumed by low-level helpers (`alternates.Env`, `GitLabHookManager.customHooksEnv`) that build `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES` via a bare `filepath.Join(repoPath, value)` with no containment check, unlike the properly-validated `Repo.ObjectDirectoryPath()` helper used elsewhere in the codebase.

### Finding Description
`Repository.GitObjectDirectory` and `Repository.GitAlternateObjectDirectories` are ordinary request fields, settable by any caller: [1](#0-0) 

When Gitaly spawns a Git command for a repository, it unconditionally injects these fields into the process environment through `alternates.Env`, which performs no boundary validation whatsoever — it simply joins the repo path with the caller-supplied (possibly relative, possibly `../`-laden) value: [2](#0-1) [3](#0-2) 

The same unsanitized propagation happens when Gitaly builds the hooks payload that is handed to `gitaly-hooks` and eventually to admin-installed custom hooks — it copies the fields verbatim from the caller-supplied `repo` object: [4](#0-3) [5](#0-4) 

By contrast, the codebase does have a correctly hardened resolver for the same fields, `Repo.ObjectDirectoryPath()`, which calls `storage.ValidateRelativePath` against the storage root and rejects traversal attempts (as proven by its extensive traversal test matrix): [6](#0-5) [7](#0-6) 

However, `alternates.Env` and `customHooksEnv` are independent code paths that do **not** go through this validation before joining the value into the Git process environment. The transaction middleware itself documents that setting these fields on an ordinary request "allows for circumventing the transaction management," and only rejects the case for mutators under transactional storage — leaving other paths (accessor RPCs, non-transactional storage, and the custom-hooks environment construction) unguarded: [8](#0-7) 

This mirrors the reported bug class: a mechanism intended only for a specific, privileged internal flow (Rails' post-quarantine access-check loop-back, analogous to the "orchestrator-only" `executeTx`) is instead reachable and honored from any ordinary caller/RPC (analogous to "any module" using `executeTxFromModule`), because the permission/validation boundary that should restrict it is missing at the actual consumption points.

### Impact Explanation
An attacker who can issue any Gitaly RPC that accepts a `Repository` message and results in a spawned Git command (or a hook execution) can set `git_object_directory`/`git_alternate_object_directories` to a path with directory-traversal sequences (e.g. `../../other-repo/objects` or an absolute path). Because `alternates.Env`/`customHooksEnv` do not validate containment within the storage root, Git may be pointed at object directories belonging to other repositories on the same storage, letting the attacker read (and in some object-pool/alternates scenarios, indirectly influence writes into) objects that do not belong to their own repository — a cross-repository object access / object-pool isolation escape. It can also leak arbitrary environment values into administrator-installed custom hook scripts.

### Likelihood Explanation
The fields are ordinary, unauthenticated (from Gitaly's perspective — any token-holding client) request fields on the common `Repository` message used across nearly all RPCs, so the reachable surface is broad. The transaction middleware's own comment confirms this is a known, currently-unaddressed gap for at least the mutator/transactional case, and the parallel, unguarded code paths (`alternates.Env`, `customHooksEnv`) indicate the gap is not fully closed elsewhere.

### Recommendation
Route `Repository.GetGitObjectDirectory()`/`GetGitAlternateObjectDirectories()` through the same `storage.ValidateRelativePath`/`ObjectDirectoryPath` containment checks before use in `alternates.Env`, `command_factory.go`'s `newCommand`, and `GitLabHookManager.customHooksEnv`, rejecting any value that resolves outside of the storage root or the expected quarantine-directory prefix, consistent with the validation already implemented in `internal/git/localrepo/paths.go`.

### Proof of Concept
Not independently executed (index-only analysis); reasoning is based on tracing the field from the proto definition, through `NewHooksPayload`/`alternates.Env`, to the unvalidated `filepath.Join` calls, contrasted with the validated `ObjectDirectoryPath` implementation and its traversal test cases cited above. A concrete PoC would involve invoking any Gitaly mutator/accessor RPC with a crafted `Repository{GitObjectDirectory: "../../<victim-repo>/objects"}` and observing that the spawned Git process's `GIT_OBJECT_DIRECTORY` environment variable resolves outside the caller's own repository.

### Citations

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

**File:** internal/gitaly/hook/custom.go (L164-190)
```go
func (m *GitLabHookManager) customHooksEnv(ctx context.Context, payload gitcmd.HooksPayload, pushOptions []string, envs []string) ([]string, error) {
	repoPath, err := m.locator.GetRepoPath(ctx, payload.Repo, storage.WithRepositoryVerificationSkipped())
	if err != nil {
		return nil, err
	}

	customEnvs := append(command.AllowedEnvironment(envs), pushOptionsEnv(pushOptions)...)

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

**File:** internal/git/localrepo/paths_test.go (L134-178)
```go
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

**File:** internal/gitaly/storage/storagemgr/middleware.go (L271-289)
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
```
