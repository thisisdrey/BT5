Found the critical piece: `alternates.Env` at line 519 of `command_factory.go` is called directly with `repo.GetGitObjectDirectory()` and `repo.GetGitAlternateObjectDirectories()` — client-controlled fields on the `Repository` protobuf message — **without any validation** for every single git command Gitaly runs, not just in the hook/quarantine flow that does validate via `ObjectDirectoryPath`/`QuarantineDirectoryPrefix`.

### Title
Unvalidated client-supplied `git_object_directory`/`git_alternate_object_directories` fields allow cross-repository object access - (File: internal/git/gitcmd/command_factory.go)

### Summary
The `Receiver` bug is fundamentally about trusting an attacker-influenced field (the callData / who it says the sender is) without verifying it actually originated from the trusted party, letting the attacker point execution at attacker-chosen data. The Gitaly analog is `ExecCommandFactory.newCommand` at `internal/git/gitcmd/command_factory.go:519`, which builds `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES` straight from `repo.GetGitObjectDirectory()` / `repo.GetGitAlternateObjectDirectories()` for **every** git invocation, regardless of RPC.

### Finding Description
Gitaly's `Repository` protobuf message carries `git_object_directory`/`git_alternate_object_directories` fields that are meant to be populated only by trusted internal callers (Rails, during quarantine bookkeeping — see `doc/object_quarantine.md`). Verification that a quarantine directory really belongs to the target repository is performed only in specific, narrow code paths: `internal/git/localrepo/paths.go` `ObjectDirectoryPath` validates the path against the repo's `storage.QuarantineDirectoryPrefix`, and `internal/gitaly/hook/prereceive.go` normalizes/derives directories relative to `repoPath` before use. [1](#0-0) [2](#0-1) 

However, the generic command-construction path used by essentially all RPCs, `ExecCommandFactory.newCommand`, takes these same fields off the incoming `Repository` message and joins them onto the repo path with no ownership check at all: [3](#0-2) 

`alternates.Env` itself performs no validation either — it simply `filepath.Join`s the repo path with whatever string is provided: [4](#0-3) 

This is analogous to the Receiver not verifying the source-chain address before trusting `_callData`: the "trusted-chain" invariant (only Rails, via the quarantine flow, should ever set these directory fields) is not enforced by the component that actually consumes the field for the majority of RPCs. An ordinary authenticated Gitaly client (any caller able to send an RPC with a `Repository` message — for example via Praefect-proxied or direct Gitaly RPCs that accept a `Repository`) can set `git_object_directory`/`git_alternate_object_directories` to an arbitrary relative (or `../`-escaping) path pointing at another repository's, or an arbitrary storage, object directory, and have Gitaly execute git commands with that alternate object directory added to the search path for object resolution.

### Impact Explanation
If reachable with an unvalidated path, this allows the classic git alternates cross-repository object disclosure: pointing `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES` at another repository (including one the caller has no permission on) lets git commands (e.g. `cat-file`, `log`, `diff`, `rev-list`) resolve and dump objects from that other repository, or use its objects to defeat correlation logic. This maps cleanly to "cross-repository object access" in the accepted-impact list.

### Likelihood Explanation
The likelihood is uncertain from static analysis alone: it depends on (a) whether any user/Praefect/Rails-reachable RPC actually plumbs a client-set `git_object_directory`/`git_alternate_object_directories` value all the way to `newCommand` without first being normalized by a stricter path like `prereceive.go`'s `getRelativeObjectDirs`, and (b) whether request validators (`internal/gitalyauth`, request-scoped repository validators) intercept and reject Repository messages carrying these fields from external gRPC callers before they reach `newCommand`. I could not fully trace every RPC entry point given the tool budget, so this should be validated by checking `protoregistry`/service-level request validation (e.g., interceptors validating `Repository` fields) and by testing whether e.g. `TreeEntry`, `Blob`, or `Commit` read RPCs pass client-supplied quarantine fields straight through.

### Recommendation
Centralize validation of `Repository.GitObjectDirectory`/`GitAlternateObjectDirectories` in `ExecCommandFactory.newCommand` itself (or in a shared pre-check invoked by it), reusing the same `storage.QuarantineDirectoryPrefix`/`ObjectDirectoryPath` ownership-hash check already used in `localrepo/paths.go`, so that *every* git invocation — not just the quarantine-aware read paths — rejects object-directory values that don't verifiably belong to the target repository.

### Proof of Concept
Not independently verified due to inability to trace every RPC's request-validation interceptor within the available tool budget; a concrete PoC would require identifying a non-mutator RPC that accepts a `Repository` message from an external, low-privilege caller, sets `git_alternate_object_directories` to a relative path escaping into another repository's storage location, and observing that `ExecCommandFactory.newCommand` (`internal/git/gitcmd/command_factory.go:519`) passes it unchecked to `alternates.Env`, followed by confirming that a read command against the target repo can now resolve objects from the pointed-at repository.

### Citations

**File:** internal/git/localrepo/paths.go (L43-73)
```go
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
