### Title
Quarantine directory validation accepts any path matching a deterministic, non-secret prefix instead of verifying it belongs to a live, currently-active quarantine — allowing stale/foreign quarantine reuse - ([File: internal/git/localrepo/paths.go])

### Summary
The Sherlock report describes `VoteTSS` authorizing a voter by checking for the **existence** of a node account rather than validating the voter's **current, live** membership in the observer set (`IsNonTombstonedObserver`/`CheckObserverCanVote`). Because node-account removal is coupled to a separate, blockable code path, a revoked identity keeps satisfying the "existence" check indefinitely. The same class of bug — validating a static/derivable identifier instead of the current liveness/ownership of the resource it names — exists in Gitaly's object-quarantine path resolution.

### Finding Description
`Repo.ObjectDirectoryPath` in [1](#0-0)  decides whether a caller-supplied `GitObjectDirectory` may be used as the Git object directory for a repository. When the path is not literally inside the repository, it falls back to checking whether the absolute path has the prefix produced by `storage.QuarantineDirectoryPrefix(repo)`: [2](#0-1) 

`QuarantineDirectoryPrefix` is **fully deterministic and non-secret** — it is simply `"quarantine-" + hex(sha1(relative_path)[:8]) + "-"`: [3](#0-2) 

This prefix depends only on the repository's `RelativePath`, which is known to anyone with access to the repository (it is sent by the client in every RPC's `Repository` message). The check therefore validates "does this path look like a quarantine directory for repo X," not "is this the specific, currently-live quarantine directory that this request's transaction created." There is no correlation to a transaction ID, PID, or any other unguessable, time-bound token — exactly the same weakness as `VoteTSS` checking presence of a node account (a durable, easily-satisfied artifact) instead of a live, per-request authorization state.

Because quarantine directories are created under the storage's shared temp directory and are only removed by an explicit `cleanup()` callback [4](#0-3) , any quarantine directory that fails to be cleaned up (crash, timeout, killed hook process, or any of the documented recovery corner cases) remains on disk indefinitely and will still pass the prefix check for that same repository on a later, unrelated request — just as a tombstoned Zetachain observer keeps satisfying `GetNodeAccount` after being removed from the active set because nothing forces re-validation of current state.

### Impact Explanation
A caller who can influence the `GitObjectDirectory`/`GitAlternateObjectDirectories` fields of a `Repository` message for their own repository (these fields are round-tripped through Rails' `/internal/allowed` access-check flow, as documented in [5](#0-4) ) can point Gitaly at a stale quarantine directory left over from a *prior, already-completed or already-rejected* push. Since object-quarantine is exactly how Gitaly keeps unpacked-but-not-yet-accepted objects from leaking into the visible repository, resurrecting an old quarantine directory can expose objects that were supposed to have been discarded when an earlier `pre-receive` hook rejected the push (object-quarantine escape), or can otherwise confuse subsequent RPCs that trust `GitObjectDirectory` in place of the caller having produced it in the same live operation.

### Likelihood Explanation
Exploitability depends on quarantine cleanup actually failing to happen (crash/timeout/race) so a stale directory persists; the prefix itself is trivially computable by anyone who knows the repository's relative path, removing any "secret" component from the check. This mirrors the report's finding that the flaw is only exploitable via an adjacent condition (observer removal without going through `updateObserver`) — likelihood is therefore moderate and conditioned on quarantine directories outliving their transaction, which the codebase acknowledges is possible (temp-dir cleanup is a best-effort, age-based background sweep rather than a guarantee tied to transaction lifetime, per [6](#0-5) ).

### Recommendation
Do not accept a quarantine path based solely on its deterministic, guessable prefix. Bind the validation to the specific quarantine directory actually created for the current transaction/request (e.g., pass and check an unguessable, per-invocation token/ID alongside the prefix, or require the directory to be registered in an in-memory/transactional table of currently-live quarantines) rather than a name pattern derived purely from public repository metadata — analogous to replacing `IsNonTombstonedObserver`/existence-only checks with a check of current, live authorization state (`CheckObserverCanVote`).

### Proof of Concept
1. Push to repository `R` such that Gitaly creates quarantine directory `T = <storage-tmp>/quarantine-<hash(R)>-XXXX` and the push is rejected by `pre-receive` (objects remain unpacked in `T`) — or the process is killed/crashes before `cleanup()` runs, per [7](#0-6) .
2. If `T` is not removed (crash, timeout, or race with the age-based sweep in `clean()` which only removes entries older than `maxAge`), it persists on disk.
3. In a later, unrelated request for the same repository `R` (e.g. through the internal API/hook flow that lets Rails set `GitObjectDirectory`/`GitAlternateObjectDirectories` back on the `Repository` message), craft/replay `GitObjectDirectory` pointing at `T`.
4. `ObjectDirectoryPath` accepts it purely because it matches the deterministic `QuarantineDirectoryPrefix(R)` string, per [8](#0-7) , without any check that `T` corresponds to a currently-active transaction — allowing access to objects that should have been discarded/isolated.

### Citations

**File:** internal/git/localrepo/paths.go (L19-45)
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
```

**File:** internal/git/localrepo/paths.go (L53-74)
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

**File:** internal/git/quarantine/quarantine.go (L35-61)
```go
// New creates a new quarantine directory and returns the directory and a cleanup function.
// The cleanup function must be called to remove the quarantine directory.
func New(ctx context.Context, repo *gitalypb.Repository, logger log.Logger, locator storage.Locator) (*Dir, func(), error) {
	repoPath, err := locator.GetRepoPath(ctx, repo, storage.WithRepositoryVerificationSkipped())
	if err != nil {
		return nil, nil, structerr.NewInternal("getting repo path: %w", err)
	}

	quarantineDir, cleanup, err := tempdir.NewWithPrefix(ctx, repo.GetStorageName(),
		storage.QuarantineDirectoryPrefix(repo), logger, locator)
	if err != nil {
		return nil, nil, fmt.Errorf("creating quarantine: %w", err)
	}

	quarantinedRepo, err := Apply(repoPath, repo, quarantineDir.Path())
	if err != nil {
		cleanup() // Clean up if we fail after creating the temp directory
		return nil, nil, err
	}

	return &Dir{
		repo:            repo,
		quarantinedRepo: quarantinedRepo,
		locator:         locator,
		dir:             quarantineDir,
	}, cleanup, nil
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

**File:** internal/tempdir/clean.go (L62-111)
```go
func clean(logger log.Logger, locator storage.Locator, storage config.Storage) error {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	dir, err := locator.TempDir(storage.Name)
	if err != nil {
		return fmt.Errorf("temporary dir: %w", err)
	}

	// If we start "cleaning up" the wrong directory we may delete user data
	// which is Really Bad.
	if !strings.HasSuffix(dir, tmpRootPrefix) {
		logger.Info(dir)
		panic(invalidCleanRoot("invalid tempdir clean root: panicking to prevent data loss"))
	}

	entries, err := os.ReadDir(dir)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}

	for _, entry := range entries {
		info, err := entry.Info()
		if err != nil {
			// It's fine if the entry has disappeared meanwhile, we wanted to remove it
			// anyway.
			if errors.Is(err, fs.ErrNotExist) {
				continue
			}

			return fmt.Errorf("statting tempdir entry: %w", err)
		}

		if time.Since(info.ModTime()) < maxAge {
			continue
		}

		fullPath := filepath.Join(dir, info.Name())

		if err := perm.FixDirectoryPermissions(ctx, fullPath); err != nil {
			return err
		}

		if err := os.RemoveAll(fullPath); err != nil {
			return err
		}
	}
```
