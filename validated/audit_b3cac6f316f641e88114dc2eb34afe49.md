### Title
Object-directory field lets an ordinary client bypass Gitaly's transactional quarantine gating and reach unvalidated storage paths - ([File: internal/gitaly/storage/storagemgr/middleware.go])

### Summary
Gitaly's transaction middleware treats any request whose `Repository.GitObjectDirectory` / `GitAlternateObjectDirectories` fields are non-empty as if it were a legitimate quarantine repository that was already validated and routed through a transaction earlier in the request lifecycle (e.g. via Rails access-check callbacks). Based only on the presence of these client-controlled fields, the middleware skips both the transaction/partition machinery **and** `locator.ValidateRepository`, and for read-only ("accessor") RPCs it forwards the request to the handler unchanged. The handler then feeds these raw, attacker-supplied strings straight into `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES` when spawning git, without applying the path-containment checks that exist elsewhere in the codebase (`localrepo.Repo.ObjectDirectoryPath`), because that validation is only invoked by one particular RPC (`GetObjectDirectorySize`), not universally before command execution.

### Finding Description
`beginTransactionForRepository` decides how to treat a request purely based on whether the object-directory fields are set: [1](#0-0) 

The comment in the code itself acknowledges the trust assumption is not actually enforced: *"This property is violated in tests which manually configure the object directory or the alternate object directory. This allows for circumventing the transaction management ... We'll leave this unaddressed for now."* For `OpMutator` RPCs this is rejected (`ErrQuarantineConfiguredOnMutator`), but for `OpAccessor` (read-only) RPCs, the request is rewritten via `restoreSnapshotRelativePath` and dispatched **without** calling `locator.ValidateRepository` and without any check that the object-directory value actually corresponds to a quarantine directory created by Gitaly for that repository.

Downstream, when a git command is spawned for that repository, the raw fields are joined directly into path-based environment variables with no containment check: [2](#0-1) [3](#0-2) 

`alternates.Env` simply does `filepath.Join(repoPath, dir)` for every entry in `GitAlternateObjectDirectories` and for `GitObjectDirectory`, with no validation that `dir` stays inside the repository/storage boundary, and no check that it corresponds to a genuine quarantine directory.

Contrast this with the one place where such validation *is* implemented, `localrepo.Repo.ObjectDirectoryPath`, which explicitly checks that the path resolves inside the storage, is either within the repo or matches the expected per-repository quarantine-directory prefix, and rejects traversal attempts: [4](#0-3) 

Test cases confirm this function is meant to reject exactly the traversal patterns (`../bazqux.git`, `objects/../..`, cross-quarantine substitution, etc.) that would otherwise be accepted by the transaction middleware / command factory: [5](#0-4) 

However, this `ObjectDirectoryPath` validation is only wired into `GetObjectDirectorySize` (`internal/gitaly/service/repository/size.go`); it is not invoked by the transaction middleware nor by the generic `ExecCommandFactory.newCommand` path used by most accessor RPCs (`ListAllCommits`, `FindCommit`, `IsAncestor`, `GetBlob`, `ListCommits`, etc.), all of which read `GetGitObjectDirectory()`/`GetGitAlternateObjectDirectories()` straight from the client-supplied `Repository` message.

This mirrors the bug class in the external report: a restriction (the ban / "requests must come from a state where the actor is authorized") is enforced along one code path (the normal report-submission flow / the transaction+`ValidateRepository` flow for ordinary requests) but is silently skipped when the request instead arrives through an alternate, attacker-reachable path (API key / a request carrying quarantine fields), because the gate assumes—rather than verifies—that only trusted internal callers can take that path.

### Impact Explanation
An ordinary, authenticated Gitaly client that can invoke any repository-scoped **accessor** RPC on a repository it has access to (e.g. via GitLab's own project it owns) can set `GitObjectDirectory`/`GitAlternateObjectDirectories` in the `Repository` message to a crafted relative or absolute path (e.g. `../../<other-repo>/objects` or an absolute path under the storage root/temp dir). Because:
1. the transaction middleware skips `ValidateRepository` and transaction/snapshot isolation for these requests, and
2. the git command factory joins the raw path into `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES` without containment checks,

the resulting git process can read objects from a directory that does not belong to the requester's own repository/partition. This is a cross-repository/cross-tenant object-access and repository-isolation bypass (an accessor RPC—normally scoped to the requester's own repository object store—can be pointed at another repository's object database on the same storage), and it also silently defeats the transactional snapshot isolation that the rest of the codebase relies on for consistency during concurrent operations.

### Likelihood Explanation
The `GitObjectDirectory` and `GitAlternateObjectDirectories` fields are ordinary, client-settable fields of the `Repository` proto message that is embedded in virtually every Gitaly RPC request. No special privilege beyond being able to call an accessor RPC (which any authorized GitLab user/project-scoped token can do) is required to set them—this matches "ordinary user's ... crafted RPC field" exactly. The code comment in `middleware.go` itself flags that the assumption is already violated by tests that "manually configure the object directory," indicating the developers are aware real requests can carry these fields without having gone through the intended quarantine-creation flow.

### Recommendation
- Do not infer trust from the mere presence of `GitObjectDirectory`/`GitAlternateObjectDirectories`; validate that the supplied paths correspond to a legitimate quarantine directory tied to an actual server-side transaction/quarantine (the same check implemented in `localrepo.Repo.ObjectDirectoryPath`) before skipping `ValidateRepository`/transaction management.
- Apply that same containment/quarantine-prefix validation universally in `ExecCommandFactory.newCommand` (or in `alternates.Env`) rather than only in the `GetObjectDirectorySize` code path, so every git invocation is protected regardless of which RPC set the object-directory fields.
- Consider removing client-settable object-directory fields from ordinary externally-facing RPC requests entirely (as the code comment suggests: "later address this by removing the options to configure object directories and alternates in a request") and instead deriving/propagating quarantine information exclusively through server-managed, opaque transaction state.

### Proof of Concept
Conceptual PoC (exact RPC choice depends on which accessor RPCs are reachable with attacker-controlled `Repository` messages in a given GitLab/Gitaly deployment):
1. As an authorized user with access to repository A (`storage/A`), call any accessor RPC (e.g. `ListAllCommits`) with:
   ```
   Repository{
     StorageName: "default",
     RelativePath: "A.git",
     GitAlternateObjectDirectories: ["../B.git/objects"]  // or an absolute path
   }
   ```
2. Because `beginTransactionForRepository` sees `GitAlternateObjectDirectories` set, it skips `locator.ValidateRepository` and transaction/snapshot setup and forwards the request as-is (`internal/gitaly/storage/storagemgr/middleware.go:271-297`).
3. `ExecCommandFactory.newCommand` builds `GIT_ALTERNATE_OBJECT_DIRECTORIES=<repoA path>/../B.git/objects` via `alternates.Env` with no containment check (`internal/git/gitcmd/command_factory.go:519`, `internal/git/alternates/alternates.go:9-27`).
4. The resulting `git` process run against repo A can now enumerate/read objects from repo B's object database, which the requester should not otherwise have direct access to—demonstrating the isolation bypass.

Note: Full confirmation that a specific externally-reachable RPC accepts and forwards these fields without any earlier request-level filtering (e.g., in Rails/Workhorse before reaching Gitaly) would require examining the calling GitLab Rails/Workhorse code, which is outside this repository; this answer is based solely on what is verifiable inside the `gitaly--004` repository.

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
