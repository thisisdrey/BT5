### Title
Quarantine-directory validation bypass via directory-name pattern matching allows cross-repository object-directory substitution - (File: internal/git/localrepo/paths.go)

### Summary
`Repo.ObjectDirectoryPath` in `internal/git/localrepo/paths.go` decides whether a caller-supplied `GitObjectDirectory` path is a legitimate transaction quarantine directory purely by checking the *basename* of the path (`"quarantine"`, or parent `"quarantine"` + `tmp_objdir*` prefix), rather than verifying that the path actually belongs to the current repository/transaction. When this superficial pattern matches, the function skips the only ownership check it has (the per-repository hashed prefix check used for "manual" quarantine directories), and returns whatever path was supplied as long as it resolves inside the storage root and exists on disk.

### Finding Description
`ObjectDirectoryPath` computes the effective object directory for a repository from the `GitObjectDirectory` field of the `gitalypb.Repository` message: [1](#0-0) 

After confirming the path resolves somewhere inside the storage (`storage.ValidateRelativePath(storagePath, …)`), it classifies the path as a "transaction quarantine dir" solely by name: [2](#0-1) 

If `isTransactionQuarantineDir` is true, the subsequent block — which is the *only* code that verifies the path is scoped to the given repository via the per-repository hashed prefix `storage.QuarantineDirectoryPrefix(repo)` — is skipped entirely: [3](#0-2) 

Contrast this with the "manual" (non-name-matching) branch, which explicitly requires the path to be under `filepath.Join(tempDir, storage.QuarantineDirectoryPrefix(repo))` — a value derived from a SHA1 hash of the repository's own relative path: [4](#0-3) 

For the name-matching branch there is no equivalent ownership proof. The project's own test suite confirms this is not merely a theoretical edge case: it explicitly builds an arbitrary directory named `quarantine` directly under the storage root (unrelated to any hashed, repo-scoped temp directory) and asserts that `ObjectDirectoryPath` accepts it as valid: [5](#0-4) 

This is directly analogous to the ERC20 report's root cause: a validity check is implemented as a coarse "does this look like the special/forbidden case" pattern match (there, address equality to a blocklist; here, "does the basename literally equal `quarantine`") instead of verifying the actual invariant that matters (there, real intended recipient semantics; here, real per-repository/per-transaction ownership of the quarantine directory). Any component that can influence the `GitObjectDirectory` field of a `Repository` message reaching this code (documented as being looped back from Rails access-check callbacks, per `doc/object_quarantine.md`) can steer object resolution to any storage-relative path with a `quarantine`-shaped name, without proving that path is tied to the calling repository or an active transaction for it: [6](#0-5) 

`GitObjectDirectory`/`GitAlternateObjectDirectories` are treated by the transaction middleware as always coming from this loop-back mechanism and are exempted from starting a new transaction, meaning requests carrying them for read-only ("Accessor") RPCs bypass normal transaction-based repository resolution and flow straight into handlers that call `ObjectDirectoryPath`: [7](#0-6) 

### Impact Explanation
If an object directory whose basename is `quarantine` (or `.../quarantine/tmp_objdir*`) exists anywhere under the same storage root — for example a leftover/legitimate quarantine staging directory belonging to a *different* repository or transaction — a request that supplies that relative path in `GitObjectDirectory` will have it accepted as the object directory for an unrelated target repository. Git commands (e.g. `cat-file`, blame, LFS pointer scanning, commit listing) subsequently executed against that repository would then resolve objects from the substituted directory via `GIT_OBJECT_DIRECTORY`, i.e. a cross-repository object-directory ownership check is bypassable by name-pattern matching alone rather than a cryptographic/identity-based check, which is the class of defect the isolation model is specifically supposed to prevent (see the two-tier design intent documented in the same file for the "manual" case).

### Likelihood Explanation
Exploitability is bounded by the requirement that a directory with the exact expected `quarantine`/`tmp_objdir*` shape must already exist under the storage root at a path reachable from the target repository's relative path — such directories are normally short-lived, per-transaction staging directories, which narrows the practical attack window. I was not able to fully confirm, within the available index, whether any RPC surface reachable by an ordinary/unauthenticated-relative-to-Gitaly caller (as opposed to only the trusted GitLab Rails/Workhorse loop-back path) can set `GitObjectDirectory` to an attacker-chosen value; the codebase's own comments state this field is intended to only be set by the Rails access-check loop-back, but no code in the reviewed paths cryptographically authenticates that the field actually originated from that flow versus any other caller of the same gRPC method.

### Recommendation
Replace the basename-only classification (`isTransactionQuarantineDir`) with a check that verifies the given `GitObjectDirectory` is genuinely scoped to the current repository/transaction — e.g., always require the same hashed-prefix (`storage.QuarantineDirectoryPrefix(repo)`) check, or an equivalent transaction-ID-bound check, regardless of whether the path's basename happens to be `quarantine`, so ownership is proven cryptographically/structurally rather than inferred from a directory name pattern.

### Proof of Concept
The existing unit test already demonstrates the accepted-but-unverified case: a directory `«storage»/tx-tmp/quarantine/tmp_objdir-incoming-Gbc29N` is created with no relation to the target repository's hashed quarantine prefix, and `ObjectDirectoryPath` still returns it successfully when it is supplied via `GitObjectDirectory`: [8](#0-7) 
This confirms the code path accepts any storage-relative, existing directory matching the `quarantine`/`tmp_objdir*` name pattern without validating it belongs to the requesting repository.

### Citations

**File:** internal/git/localrepo/paths.go (L19-52)
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
```

**File:** internal/git/localrepo/paths.go (L53-83)
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

	fullPath := filepath.Join(repoPath, objectDirectoryPath)
	if _, err := os.Stat(fullPath); os.IsNotExist(err) {
		return "", structerr.NewNotFound("object directory does not exist: %q", fullPath)
	}

	return fullPath, nil
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

**File:** internal/git/localrepo/paths_test.go (L81-119)
```go
	// Transactions store their set a quarantine directory in the transaction's temporary
	// directory with a path ending in `quarantine` directory. Emulate that by creating
	// such a directory in the root of the storage.
	transactionStateDir := filepath.Join(cfg.Storages[0].Path, "tx-tmp")
	transactionQuarantineDir := filepath.Join(transactionStateDir, "quarantine")
	transactionQuarantineDirWithGitPush := filepath.Join(transactionQuarantineDir, "tmp_objdir-incoming-Gbc29N")
	require.NoError(t, os.MkdirAll(transactionQuarantineDirWithGitPush, mode.Directory))
	transactionQuarantineDirRelativePath, err := filepath.Rel(repoPath, transactionQuarantineDir)
	require.NoError(t, err)
	transactionQuarantineDirWithGitPushRelativePath, err := filepath.Rel(repoPath, transactionQuarantineDirWithGitPush)
	require.NoError(t, err)

	repoWithGitObjDir := func(repo *gitalypb.Repository, dir string) *gitalypb.Repository {
		repo = proto.Clone(repo).(*gitalypb.Repository)
		repo.GitObjectDirectory = dir
		return repo
	}

	testCases := []struct {
		desc string
		repo *gitalypb.Repository
		path string
		err  codes.Code
	}{
		{
			desc: "storages configured",
			repo: repoWithGitObjDir(repoProto, "objects/"),
			path: filepath.Join(repoPath, "objects/"),
		},
		{
			desc: "repo quarantined by transaction manager",
			repo: repoWithGitObjDir(quarantinedRepo, transactionQuarantineDirRelativePath),
			path: transactionQuarantineDir,
		},
		{
			desc: "repo quarantined by transaction manager additionally quarantined by git push",
			repo: repoWithGitObjDir(quarantinedRepo, transactionQuarantineDirWithGitPushRelativePath),
			path: transactionQuarantineDirWithGitPush,
		},
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
