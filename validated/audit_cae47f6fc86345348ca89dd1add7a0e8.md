### Title
Deterministic (Non-Random) Quarantine Directory Prefix Enables Quarantine-Directory Confusion Across Concurrent Operations - ([File: internal/gitaly/storage/locator.go])

### Summary
`storage.QuarantineDirectoryPrefix` is meant to act as the *unforgeable* proof that a client-supplied `GitObjectDirectory` really is a quarantine directory created by Gitaly for the repository in the request. Instead of embedding any per-operation secret/randomness, the prefix is computed purely as a deterministic SHA1 hash of the repository's `RelativePath` — data the requester already controls/knows. This mirrors the reported bug class: a security-relevant value that is supposed to prevent manipulation is derived from a predictable, attacker-known input rather than an unpredictable, per-operation random value.

### Finding Description
`QuarantineDirectoryPrefix` computes a fixed 16-hex-character prefix solely from `repo.GetRelativePath()`: [1](#0-0) 

This prefix is used by `quarantine.New` when creating the actual quarantine directory via `tempdir.NewWithPrefix`: [2](#0-1) 

`tempdir.NewWithPrefix` in turn calls `os.MkdirTemp(root, prefix)`, which appends Go's own random suffix to the prefix to create the directory: [3](#0-2) 

Crucially, the *verification* logic in `Repo.ObjectDirectoryPath` — used to decide whether a client-supplied `GitObjectDirectory` (sent back to Gitaly by Rails/GitLab access-check calls, or by any RPC caller) is a legitimate quarantine directory for the target repository — only checks a `strings.HasPrefix` match against this deterministic value: [4](#0-3) 

Because `expectedQuarantinePrefix` depends solely on `RelativePath` (public/known request data) and not on any secret or per-request nonce, *any* quarantine directory ever created for that repository — from any push, fetch, or diff RPC, by any user/session, at any point in time — satisfies the prefix check. The only thing distinguishing one quarantine instance from another is the random suffix appended by `os.MkdirTemp`, but the verification code never checks that suffix corresponds to the current operation; it merely confirms the directory exists under the expected deterministic prefix and is a subpath of the repository/temp dir (see `storage.ValidateRelativePath` calls at the top of the function).

This is analogous to the reported issue: instead of tying the security decision to a genuinely unpredictable, per-request value (the "user-provided random number" the report calls for), Gitaly relies on a value that is fully computable in advance from data already known to any caller of the repository's RPCs.

### Impact Explanation
An attacker or a misbehaving/compromised client component that can issue repository RPCs carrying a custom `GitObjectDirectory` (as Rails does after receiving quarantine information from a `pre-receive` hook, per `doc/object_quarantine.md`) can point that field at a *different* quarantine directory belonging to the same repository — e.g., one left over from a concurrent or previous (possibly rejected) push, fetch, or diff-blob operation — as long as it happens to satisfy the deterministic prefix. Since the objects in someone else's rejected/in-flight quarantine directory would then be treated as the caller's own quarantined objects, this can lead to:
- Disclosure of objects from another operation's rejected/pending push (an object confusion / minor information disclosure) on the same repository.
- Confusing housekeeping/read RPCs (e.g., `GetObjectDirectorySize`, `DiffBlobs`) into operating on the wrong quarantine data.

The blast radius is limited to quarantines of the *same* repository (the hash is repository-scoped), so this is not a full cross-repository escape, but it does defeat the stated security purpose of the prefix ("verify that a given quarantine object directory indeed belongs to the repository at hand") because the check cannot distinguish between different quarantine instances of that repository.

### Likelihood Explanation
Exploitation requires: (1) knowledge of the target repository's `RelativePath` (routinely available to any caller with access to the repo, since it's a required RPC field), and (2) the ability to cause the deterministic-prefix directory validation path to be exercised with a controlled `GitObjectDirectory`/`GitAlternateObjectDirectories` value (reachable via any RPC that accepts a `Repository` message with these fields set, as exercised in `TestGetObjectDirectorySize_quarantine`). No cryptographic secret needs to be guessed — only the presence of *some* existing quarantine directory for the repository (which naturally accumulates over time from prior git operations before cleanup).

### Recommendation
Do not rely on a value derived purely from public/known repository metadata to authenticate a quarantine directory. Instead, generate and track a per-operation, cryptographically random quarantine token (e.g., via `text.RandomHex`, already used elsewhere for object-pool backup files) and pass it explicitly through the RPC/transaction context so verification can match on that secret component in addition to (or instead of) the deterministic repository-scoped prefix. This restores the "user-provided" unpredictable component so that quarantine identity cannot be inferred or confused across concurrent operations, restoring the same protection model the Entropy-analog report recommends: unpredictability must come from something the requester of the *specific verification* actually possesses, not from data that's already public.

### Proof of Concept
1. Trigger multiple concurrent write operations (e.g., pushes) against the same repository `R` so multiple quarantine directories `tempdir/quarantine-<hash(R.RelativePath)>-<rand1>` and `.../-<rand2>` exist simultaneously (this happens naturally under load, or a rejected `pre-receive` push leaves its quarantine directory around before cleanup completes).
2. As a component/caller that legitimately knows `R.RelativePath` (e.g., a Rails access-check callback), craft a `Repository` message for a *different* in-flight operation on `R`, but set `GitObjectDirectory` to the *other* operation's quarantine relative path.
3. Call an RPC such as `GetObjectDirectorySize` or any code path hitting `Repo.ObjectDirectoryPath` — verification succeeds via `QuarantineDirectoryPrefix` prefix match at [5](#0-4)  even though the directory belongs to an unrelated operation, since the check never verifies the random suffix ties back to the current request/transaction.

### Citations

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

**File:** internal/git/quarantine/quarantine.go (L35-47)
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
```

**File:** internal/tempdir/tempdir.go (L82-96)
```go
func newDirectory(ctx context.Context, storageName string, prefix string, logger log.Logger, loc storage.Locator) (Dir, error) {
	root, err := loc.TempDir(storageName)
	if err != nil {
		return Dir{}, fmt.Errorf("temp directory: %w", err)
	}

	if err := os.MkdirAll(root, mode.Directory); err != nil {
		return Dir{}, err
	}

	tempDir, err := os.MkdirTemp(root, prefix)
	if err != nil {
		return Dir{}, err
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
