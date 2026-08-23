### Title
Cross-Repository Object Directory Quarantine Check Bypass via "quarantine"-named Path Component - ([File: internal/git/localrepo/paths.go])

### Summary
`Repo.ObjectDirectoryPath` implements two competing validation checks for the same invariant ("the client-supplied `GitObjectDirectory` must actually belong to *this* repository's own quarantine, not some arbitrary location"), and the two checks disagree on edge cases in a way that lets the weaker check silently short-circuit the stronger one.

### Finding Description
`ObjectDirectoryPath` first validates that the client-supplied `objectDirectoryPath` resolves to *somewhere inside the storage root* (not necessarily inside the target repository): [1](#0-0) 

It then derives `isTransactionQuarantineDir` purely from the *name* of the last path component of that storage-relative path — `quarantine`, or `tmp_objdir*` under a parent literally named `quarantine`: [2](#0-1) 

If that heuristic matches, the function completely skips the second, stricter check that actually verifies the path belongs to the requested repository (either as a path within `repoPath` itself, or as a directory under the storage's temp dir whose prefix is `QuarantineDirectoryPrefix(repo)`, a SHA1-keyed prefix derived from the repository's own relative path): [3](#0-2) 

The first check (`storage.ValidateRelativePath(storagePath, ...)`) only enforces "stay inside the storage," matching the "weak/accepting" validation path in the reported bug class (analogous to `calculateRootPaths` accepting the edge case). The second check (`storage.ValidateRelativePath(repoPath, ...)` plus the `QuarantineDirectoryPrefix` comparison) is the "strict/rejecting" validation (analogous to `_validatePathLengthForSingleProof`). Because `isTransactionQuarantineDir` is derived only from a directory basename match rather than from a repository-scoped check, a `GitObjectDirectory` value such as `../<any-other-repo-relative-path>/quarantine` (or `.../quarantine/tmp_objdir-x`) that resolves to a directory inside the storage root but *outside* `repoPath` and outside this repo's own temp-quarantine prefix will still set `isTransactionQuarantineDir = true` and skip the ownership check entirely, as long as the final path component is literally named `quarantine` (or the `tmp_objdir*`/`quarantine` combination) — a name an attacker fully controls.

`GitObjectDirectory` is a client-settable field on the `Repository` protobuf message that flows directly into this function without any check that it originated from Gitaly's own quarantine bookkeeping; the existing code comment even acknowledges "this property is violated in tests which manually configure the object directory... this allows for circumventing the transaction management." Any RPC that resolves the object directory via `Repo.ObjectDirectoryPath` — e.g. `GetObjectDirectorySize` — directly exercises this path: [4](#0-3) 

### Impact Explanation
An attacker who can influence the `GitObjectDirectory` field of a `Repository` message (an ordinary user-controllable protobuf field) can construct a relative path that: (1) stays within the storage root (satisfying check 1), and (2) ends in a component literally named `quarantine`, pointing at an arbitrary other repository's or arbitrary storage-internal directory, thereby bypassing the repository-ownership verification meant to gate object-directory/quarantine access. This grants cross-repository object-directory access within the same storage (e.g., disclosure of object directory sizes/content of unrelated repositories via `GetObjectDirectorySize` or other RPCs that read from the resolved path), defeating the quarantine-isolation invariant the second check exists to enforce.

### Likelihood Explanation
Reachable directly through a standard, unprivileged gRPC request by simply populating `Repository.GitObjectDirectory` with an attacker-chosen value ending in `quarantine`; no authentication bypass, special timing, or malicious peer is required — only crafting a specific field value on a request that already accepts this field on the wire. The main precondition is knowledge of another repository's relative path within the same storage, which is often derivable/enumerable.

### Recommendation
Remove the basename-only shortcut (`isTransactionQuarantineDir`) as an exemption from ownership validation. Instead, always require that the resolved path either (a) lies within `repoPath`, or (b) lies within the storage's temp directory under this specific repository's `QuarantineDirectoryPrefix`. Do not let the literal string `quarantine`/`tmp_objdir` in the path's basename alone determine trust; unify the two checks into a single strict validator that always enforces repository ownership regardless of naming.

### Proof of Concept
1. Create repository A (target) and repository B (attacker-observable, arbitrary) in the same Gitaly storage.
2. Issue a `GetObjectDirectorySize` (or any RPC resolving `ObjectDirectoryPath`) request for repository A with `Repository.GitObjectDirectory` set to a relative path such as `../<B's-relative-path>/objects/quarantine`.
3. `storage.ValidateRelativePath(storagePath, filepath.Join(repoPathA, objectDirectoryPath))` succeeds because the resolved path is still inside the storage root.
4. `filepath.Base(relativeObjectDirectoryPath) == "quarantine"` makes `isTransactionQuarantineDir = true`, skipping the ownership check that would otherwise reject this path (it does not belong to repo A nor to repo A's own temp-quarantine prefix).
5. The function returns the path pointing into repository B's object directory tree, which is then walked/sized (or otherwise used) as if it belonged to repository A, disclosing information about a repository the caller may not have legitimate scoped access to.

### Citations

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

**File:** internal/git/localrepo/paths.go (L43-52)
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

**File:** internal/gitaly/service/repository/size.go (L39-57)
```go
func (s *server) GetObjectDirectorySize(ctx context.Context, in *gitalypb.GetObjectDirectorySizeRequest) (*gitalypb.GetObjectDirectorySizeResponse, error) {
	repository := in.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repository); err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}
	repo := s.localRepoFactory.Build(repository)

	path, err := repo.ObjectDirectoryPath(ctx)
	if err != nil {
		return nil, err
	}
	// path is the objects directory path, not repo's path
	sizeInBytes, err := dirSizeInBytes(path, snapshot.NewDefaultFilter(ctx))
	if err != nil {
		return nil, fmt.Errorf("calculating directory size: %w", err)
	}

	return &gitalypb.GetObjectDirectorySizeResponse{Size: sizeInBytes / 1024}, nil
}
```
