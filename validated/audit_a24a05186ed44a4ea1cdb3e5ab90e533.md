### Title
`quarantine.Apply` folds unsanitized `Repository.GitObjectDirectory`/`GitAlternateObjectDirectories` into the quarantined repo's alternates, enabling cross-repository object disclosure - ([File: internal/git/quarantine/quarantine.go])

### Summary
`quarantine.Apply` builds the returned `quarantinedRepo.GitAlternateObjectDirectories` list by directly appending `repo.GetGitObjectDirectory()` and `repo.GetGitAlternateObjectDirectories()` with no path validation, unlike `internal/git/localrepo/paths.go`'s `ObjectDirectoryPath`, which calls `storage.ValidateRelativePath` and enforces the quarantine-prefix invariant. Because `internal/git/gitcmd/command_factory.go`'s `newCommand` sets `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES` straight from these unvalidated fields, an attacker-controlled `Repository.GitObjectDirectory` value containing `../` traversal can point Git's alternate object search at a sibling repository's object store.

### Finding Description
`quarantine.Apply` (internal/git/quarantine/quarantine.go:65-88) computes the new quarantine object directory safely via `filepath.Rel(repoPath, quarantineDir)` (Gitaly-generated, not attacker-influenced), but it takes the *pre-existing* object directory straight from client input: [1](#0-0) 

`objectDir := repo.GetGitObjectDirectory()` and `repo.GetGitAlternateObjectDirectories()...` are appended into `quarantinedRepo.GitAlternateObjectDirectories` with no call to `storage.ValidateRelativePath` or any check that the value stays inside the repository/storage root. This contrasts with the only other consumer of these fields that does perform such validation, `Repo.ObjectDirectoryPath` in `internal/git/localrepo/paths.go`, which explicitly validates via `storage.ValidateRelativePath(storagePath, ...)` and further restricts non-quarantine paths to be inside the repo: [2](#0-1) 

The resulting `quarantinedRepo` (with the tainted `GitAlternateObjectDirectories`) is subsequently used to run Git commands. `ExecCommandFactory.newCommand` reads these fields directly and passes them to `alternates.Env` to build `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES` environment variables with no intervening path validation: [3](#0-2) 

`Repository` is a plain protobuf message; `GitObjectDirectory` and `GitAlternateObjectDirectories` are ordinary client-settable fields (`internal/gitaly/storage/locator.go` defines the `Repository` interface exposing `GetGitObjectDirectory`/`GetGitAlternateObjectDirectories`), and no interceptor or `ValidateRepository`/`GetRepoPath` check found in this codebase rejects a non-empty, traversal-containing value for these two fields before it reaches `quarantine.New`/`Apply`. `locator.GetRepoPath` only validates `RelativePath`/`StorageName`, not these auxiliary object-directory fields.

Exploit flow: an unprivileged user calls an RPC that quarantines the target repository (e.g. `FetchRemote`, `ReceivePack`) supplying `Repository{RelativePath: "attacker-owned.git", GitObjectDirectory: "../victim-repo.git/objects"}`. `quarantine.New`→`Apply` folds this into `quarantinedRepo.GitAlternateObjectDirectories`, and every subsequent git invocation performed with `quarantinedRepo` (e.g. object lookups during the RPC, `git-cat-file`, `git-log`) sets `GIT_ALTERNATE_OBJECT_DIRECTORIES` to the escaped path, causing Git to also resolve/serve objects from the victim repository's object store.

### Impact Explanation
This allows cross-repository object disclosure: an attacker who controls a repository they own can cause Git to treat another repository's object directory as an alternate object source for their own quarantined operations, letting them read objects (blobs/commits/trees) that exist only in the victim repository, provided they can guess or otherwise learn valid object IDs. This matches the "cross-repository object access" impact class explicitly called out as in-scope.

### Likelihood Explanation
Preconditions are minimal for an unprivileged, self-service GitLab user: own/push/fetch access to one repository and the ability to invoke an RPC (e.g. an internal/replication-adjacent RPC or fetch path) that constructs a `Repository` message from client input and calls `quarantine.New`/`Apply` on it, with `GitObjectDirectory` left under attacker control. No admin role, stolen secret, or peer compromise is required — only crafting a protobuf field with a `../` relative path, which is standard client behavior against a gRPC field with no dedicated server-side validation in this code path.

### Recommendation
In `quarantine.Apply`, validate `repo.GetGitObjectDirectory()` and each entry of `repo.GetGitAlternateObjectDirectories()` with `storage.ValidateRelativePath(repoPath, ...)` (mirroring `Repo.ObjectDirectoryPath`'s logic, including the quarantine-prefix allowance) before folding them into `quarantinedRepo.GitAlternateObjectDirectories`, rejecting any path that escapes the repository or an already-verified quarantine directory. Additionally, `ExecCommandFactory.newCommand` should not trust `repo.GetGitObjectDirectory()`/`GetGitAlternateObjectDirectories()` verbatim for environment construction; route them through the same validated resolution used by `ObjectDirectoryPath`.

### Proof of Concept
```go
func TestApply_RejectsPathTraversalInGitObjectDirectory(t *testing.T) {
	ctx := testhelper.Context(t)
	cfg := testcfg.Build(t)
	repoProto, repoPath := gittest.CreateRepository(t, ctx, cfg)

	// Attacker-controlled repository message with a traversal payload.
	repoProto.GitObjectDirectory = "../other-repo.git/objects"

	quarantineDir := filepath.Join(filepath.Dir(repoPath), "tmp", "quarantine-xyz")

	quarantinedRepo, err := quarantine.Apply(repoPath, repoProto, quarantineDir)
	require.NoError(t, err) // currently succeeds — no validation performed

	// Expectation for a fix: the escaping alternate should be rejected.
	require.NotContains(t, quarantinedRepo.GetGitAlternateObjectDirectories(), "../other-repo.git/objects")
}
```
Expected (fixed) behavior: `Apply` returns an error (e.g. wrapping `storage.ErrRelativePathEscapesRoot`) instead of silently propagating the traversal path into `quarantinedRepo.GitAlternateObjectDirectories`.

### Citations

**File:** internal/git/quarantine/quarantine.go (L71-85)
```go
	// All paths are relative to the repository root.
	objectDir := repo.GetGitObjectDirectory()
	if objectDir == "" {
		// Set the default object directory as an alternate if the repository didn't
		// have the object directory overwritten yet.
		objectDir = "objects"
	}

	alternateObjectDirs := make([]string, 0, len(repo.GetGitAlternateObjectDirectories())+1)
	alternateObjectDirs = append(alternateObjectDirs, objectDir)
	alternateObjectDirs = append(alternateObjectDirs, repo.GetGitAlternateObjectDirectories()...)

	quarantinedRepo := proto.Clone(repo).(*gitalypb.Repository)
	quarantinedRepo.GitObjectDirectory = relativePath
	quarantinedRepo.GitAlternateObjectDirectories = alternateObjectDirs
```

**File:** internal/git/localrepo/paths.go (L36-74)
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
