### Title
Unvalidated `GitObjectDirectory`/`GitAlternateObjectDirectories` fields let a request "repository" be authorized under one path while Git objects are actually read from an attacker-chosen path - ([File: internal/git/gitcmd/command_factory.go])

### Summary
The `Repository` protobuf message carries two independent "addresses" for the same logical repository: the `storage_name`/`relative_path` pair, which is what Gitaly authorizes/validates against, and `git_object_directory` / `git_alternate_object_directories`, which is what Git actually uses to locate objects when a command is spawned. These two addresses are not kept in sync by a common validation path, so a caller can make the authorized address and the value-resolving address diverge — the same bug class as the ERC20 "multiple addresses" report, where a check is performed against one address of an asset while the balance/value is drawn from a different address for the same asset.

### Finding Description
When Gitaly builds a Git command for a `Repository`, it resolves the on-disk path via `cf.locator.GetRepoPath(ctx, repo)` (which validates `storage_name`/`relative_path` against the storage root), but then unconditionally derives the object-search environment straight from client-controlled fields: [1](#0-0) 

That environment is built by `alternates.Env`, which simply joins the repo path with whatever `objectDirectory`/`alternateObjectDirectories` strings were supplied — with no check that the result stays inside the storage root: [2](#0-1) 

By contrast, the one place that *does* validate this same field, `(*Repo).ObjectDirectoryPath`, explicitly calls `storage.ValidateRelativePath` and rejects paths that escape the repository/storage boundary or that aren't a recognized quarantine directory: [3](#0-2) 

So there exist two divergent code paths for the *same fields* on the *same* `Repository` message: one path (`ObjectDirectoryPath`) enforces containment, the other (`newCommand` → `alternates.Env`) does not. The `beginTransactionForRepository` transaction-management code even documents that setting these object-directory fields is a known way to route around the normal storage/relative-path validation and transaction machinery, restricting only mutators while leaving accessors unprotected: [4](#0-3) 

This is analogous to the reported vault flaw: the authorization/weight check is performed against one identifier (`relative_path`, validated by `locator.ValidateRepository`/`GetRepoPath`), while the value actually consumed by the underlying engine (Git's object resolution, driven by `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES`) is sourced from a second, unvalidated identifier for the "same" repository.

### Impact Explanation
An accessor RPC that accepts a `Repository` message with attacker-influenced `git_object_directory` / `git_alternate_object_directories` (e.g., relayed through Rails' `/allowed` callback loop or any client capable of populating these fields, as documented in `object_quarantine.md`) can point Git's object search path at an arbitrary directory on disk (via `../` traversal) that is outside the intended repository or even outside the storage root altogether. Because `GIT_ALTERNATE_OBJECT_DIRECTORIES` makes Git treat the pointed-at directory's objects as though they belong to the target repository, this enables cross-repository object disclosure — reading blobs/commits from a different repository (or storage) than the one the RPC was authorized against — while all path/storage validation logged for the request still reports the original, legitimate `relative_path`.

### Likelihood Explanation
Reaching this requires an RPC where the `Repository.git_object_directory`/`git_alternate_object_directories` fields are attacker/ordinary-user controllable and processed by `newCommand` without going through `ObjectDirectoryPath`'s containment check. This is a narrower, request-crafting scenario (not the common push/fetch path), but the code comment in `middleware.go` explicitly acknowledges this gap is currently unaddressed, and the divergent behavior between the two internal helper functions is objectively present in the code today.

### Recommendation
Enforce the same `storage.ValidateRelativePath` containment check used in `ObjectDirectoryPath` before `alternates.Env` is used to build the Git command environment in `newCommand`, for both `GitObjectDirectory` and every entry of `GitAlternateObjectDirectories`. Reject requests whose resolved object directory falls outside the storage root or outside the recognized quarantine-directory shape, symmetrically for accessor and mutator RPCs, rather than only gating mutators as done today in `beginTransactionForRepository`.

### Proof of Concept
1. Craft an RPC request with `Repository{storage_name: "default", relative_path: "victim-repo.git", git_alternate_object_directories: ["../../../other-storage/other-repo.git/objects"]}` targeting an accessor RPC (e.g., an object-reading RPC) that is not gated by `beginTransactionForRepository`'s mutator-only quarantine check.
2. `locator.ValidateRepository`/`GetRepoPath` validates and returns the legitimate path for `victim-repo.git`.
3. `newCommand` calls `alternates.Env(repoPath, "", ["../../../other-storage/other-repo.git/objects"])`, producing `GIT_ALTERNATE_OBJECT_DIRECTORIES=<repoPath>/../../../other-storage/other-repo.git/objects` with no containment check, unlike `ObjectDirectoryPath`.
4. The spawned Git process can now resolve and return objects belonging to `other-repo.git`, even though the RPC was only authorized for `victim-repo.git`.

### Citations

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

**File:** internal/git/localrepo/paths.go (L27-41)
```go
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
