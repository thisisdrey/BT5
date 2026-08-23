### Title
Quarantine directory validation gate uses a hardcoded name-based check that is not tied to the requesting repository, allowing cross-repository quarantine directory access - (File: internal/git/localrepo/paths.go)

### Summary
`Repo.ObjectDirectoryPath` validates the client-supplied `GitObjectDirectory` field of a `gitalypb.Repository` message before using it to build a filesystem path for Git operations. Analogous to the reported bug class (a hardcoded constant/assumption substituting for a value that should be validated per-instance), this function uses a hardcoded literal name check (`"quarantine"` / `"tmp_objdir"` prefix) to decide whether a supplied object directory is a legitimate transaction quarantine directory, instead of verifying that the quarantine directory actually belongs to the specific repository making the request.

### Finding Description
`ObjectDirectoryPath` first validates that the resolved directory is within the storage root, then branches into two validation paths depending on whether the directory *looks like* a quarantine directory: [1](#0-0) 

- If `isTransactionQuarantineDir` is true (i.e., the base directory is literally named `"quarantine"`, or the parent directory is named `"quarantine"` and the base directory starts with `"tmp_objdir"`), **no further check is performed** — the path is accepted purely because of this hardcoded name pattern.
- Only in the `else` branch (i.e., when the name does *not* match this pattern) does the code perform a repository-specific check, using `storage.QuarantineDirectoryPrefix(repo)` — a hash derived from the repository's own relative path — to confirm the manual quarantine directory actually belongs to *this* repository: [2](#0-1) 

The repository-specific ownership check (`QuarantineDirectoryPrefix`) exists specifically because quarantine directories for different repositories live in a shared, per-storage temporary directory (see `tempdir.NewWithPrefix` combined with `storage.QuarantineDirectoryPrefix(repo)` in quarantine creation): [3](#0-2) 

However, the `isTransactionQuarantineDir` shortcut bypasses this ownership check entirely. It relies solely on the literal string `"quarantine"` matching the base or parent directory name — a hardcoded pattern that does not vary per repository, per transaction, or per storage configuration. Since transaction-created quarantine directories in the storage's temp directory are simply named `"quarantine"` (as also referenced in `doc/object_quarantine.md`), any client that can set `GitObjectDirectory` to a relative path resolving to *any* directory under the storage temp tree that is named `"quarantine"` (or `.../quarantine/tmp_objdir-*`) — regardless of which repository's transaction created it — will pass validation without the per-repo hash-prefix check.

This is the same root-cause pattern as the reported bug: a hardcoded, non-configurable assumption (fee = 997 in the original report; directory name = `"quarantine"` here) is used in place of a value that should instead be resolved/verified per-instance (the pool's actual fee; the actual owning repository of the quarantine directory).

### Impact Explanation
If an attacker (or a compromised internal caller, e.g., a malicious value smuggled through Rails' object-quarantine metadata pass-through described in `doc/object_quarantine.md`) can influence the `GitObjectDirectory` field of a `Repository` message sent on a Gitaly RPC, they could point it at another repository's concurrently-active transaction quarantine directory (which lives in a shared per-storage temp directory, differentiated normally only by the repo-specific hash prefix for *manual* quarantines, but not enforced at all for the generic `"quarantine"`-named ones). This would let Gitaly serve or expose objects belonging to another repository's in-flight push (which has not yet passed access checks / been migrated into the main odb) — a cross-repository object access / information-disclosure condition, since `ObjectDirectoryPath` is used across repository RPCs to resolve the object directory used for subsequent Git operations.

### Likelihood Explanation
Exploitation requires being able to set the `GitObjectDirectory` field to a crafted relative path and knowing/guessing another repository's active quarantine temp-directory name (which, per the shortcut logic, only needs to be literally named `"quarantine"` or a `"quarantine/tmp_objdir-*"` child — there is no repo-specific secret embedded in that name, unlike the manual-quarantine hash prefix). This lowers the bar for an attacker who already has some level of RPC access (e.g., via the internal Rails-Gitaly quarantine metadata channel described in `doc/object_quarantine.md`) compared to guessing the repo-specific hash prefix required in the other branch.

### Recommendation
Remove the hardcoded name-based shortcut (`isTransactionQuarantineDir`) as an unconditional bypass. Instead, always verify that the resolved quarantine directory belongs to the requesting repository/transaction — e.g., by checking that the directory is within the specific transaction's scoped temp directory for *this* repository (not just any storage-wide directory literally named `"quarantine"`), consistent with how the manual quarantine branch already validates ownership via `storage.QuarantineDirectoryPrefix(repo)`.

### Proof of Concept
Conceptual PoC (not executed, reasoning based on code above):
1. Repository A's push creates a transaction and its quarantine directory ends up as `<storage-temp>/<txn-A-scope>/quarantine`.
2. An attacker issues an RPC against Repository B (or an access-check callback with attacker-influenced quarantine metadata as described in `doc/object_quarantine.md`), setting `GitObjectDirectory` on Repository B's message to a relative path that resolves to `<storage-temp>/<txn-A-scope>/quarantine`.
3. In `ObjectDirectoryPath`, `relativeObjectDirectoryPath` resolves under the storage root successfully (the temp dir is inside the storage), `baseDir == "quarantine"` is true, so `isTransactionQuarantineDir` short-circuits and the per-repo ownership check (`QuarantineDirectoryPrefix`) is never invoked.
4. Subsequent Git operations against Repository B now read from Repository A's in-flight, unverified quarantine object directory.

Note: I could not fully trace every caller that sets `GitObjectDirectory` on inbound RPCs (e.g., whether Praefect/Rails validates this field before forwarding) within the indexed portion of the codebase, so the precise reachability from an *external, unauthenticated* actor is not fully confirmed — it is confirmed reachable from the internal Rails/Gitaly quarantine-metadata pass-through path described in `doc/object_quarantine.md`.

### Citations

**File:** internal/git/localrepo/paths.go (L43-75)
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
		}
	}
```

**File:** internal/git/quarantine/quarantine.go (L37-47)
```go
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
