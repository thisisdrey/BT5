### Title
Object-directory quarantine ownership check uses unbounded string-prefix match, allowing cross-repository object-directory acceptance - (File: internal/git/localrepo/paths.go)

### Summary
`Repo.ObjectDirectoryPath()` validates a caller-supplied `GitObjectDirectory` field on a `gitalypb.Repository` message. When the path does not resolve inside the repository itself, the code falls back to checking whether it is a "manual" quarantine directory belonging to the same repository by comparing it against an `expectedQuarantinePrefix` using a bare `strings.HasPrefix()` call, exactly the same bug class as `OracleUtils.ethLeftSide()`: a prefix is checked without verifying that a delimiter boundary follows it, so a string that merely starts with the same characters is accepted as if it were the same, well-defined entity.

### Finding Description
In `ObjectDirectoryPath()`: [1](#0-0) 

```go
if _, origError := storage.ValidateRelativePath(repoPath, objectDirectoryPath); origError != nil {
    tempDir, err := repo.locator.TempDir(repo.GetStorageName())
    ...
    expectedQuarantinePrefix := filepath.Join(tempDir, storage.QuarantineDirectoryPrefix(repo))
    absoluteObjectDirectoryPath := filepath.Join(repoPath, objectDirectoryPath)

    // The relative path is outside of the repository
    if !strings.HasPrefix(absoluteObjectDirectoryPath, expectedQuarantinePrefix) {
        return "", structerr.NewInvalidArgument("not a valid relative path: %w", origError)
    }
}
```

`expectedQuarantinePrefix` is derived from `storage.QuarantineDirectoryPrefix(repo)`, a per-repository prefix string that is joined with the storage's temp directory but is **not** followed by a path separator before the `HasPrefix` comparison. This is the same defect class as the reported `ethLeftSide()` bug: checking a short prefix of a string without confirming a boundary/delimiter character follows it, so a *different* but similarly-prefixed string is misclassified as belonging to the checked category.

Concretely, Gitaly's quarantine/temp directories created via `tempdir.NewWithPrefix()` (used by `quarantine.New()`, called for every `git-receive-pack`/`git-upload-pack` push and fetch operation) generate directory names that start with a fixed prefix and append a random suffix (analogous to `os.MkdirTemp`-style naming: `prefixXXXXXX`). Because `expectedQuarantinePrefix` for repository A is only a string prefix and not bounded by a path separator, a quarantine directory belonging to a *different* repository B whose generated random-suffixed name happens to literally start with repository A's prefix string (e.g., repo A's prefix is `quarantine-abc` and repo B's actual directory is `quarantine-abc123-def/`) would satisfy `strings.HasPrefix()` even though it is not repository A's directory and is not even a subdirectory of it. [2](#0-1) 

By contrast, the correctly-bounded analog exists a few lines away for the storage-root check in `internal/git/dirs.go`, which explicitly appends `os.PathSeparator` to the prefix before comparing: [3](#0-2) [4](#0-3) 

The `paths.go` quarantine check omits this safeguard, making it the vulnerable analog.

### Impact Explanation
If an attacker (an ordinary authenticated Gitaly client, e.g., through a push/fetch RPC that lets the client control `GitObjectDirectory`/`GitAlternateObjectDirectories` fields on the `Repository` message, such as `PostReceivePack`/`SSHReceivePack`-style flows or any RPC that echoes back a client-supplied quarantine object directory) can craft an `objectDirectoryPath` value whose absolute path merely shares a prefix string with another repository's legitimately-created quarantine directory, `ObjectDirectoryPath()` will incorrectly accept it as valid for the current repository. This could let Gitaly read/write objects from/into a quarantine directory that does not actually belong to the calling repository, resulting in cross-repository object directory confusion — objects from an unrelated repository's in-flight push could be exposed to, or merged into, another repository's transaction.

### Likelihood Explanation
Exploitation requires that two repositories' temp/quarantine directory names collide on a prefix basis at the same point in time (both being simultaneously in a push/quarantine phase) and that an attacker can supply an `objectDirectoryPath` value in a request. Because quarantine directory suffixes are randomly generated, natural collision is unlikely, but the check should never rely on probabilistic non-collision for a security boundary — the correct fix (append `string(os.PathSeparator)` to `expectedQuarantinePrefix`, mirroring `internal/git/dirs.go`) is cheap and eliminates the class entirely, matching the sherlock report's proposed mitigation of checking for an explicit delimiter rather than a bare prefix.

### Recommendation
Change the comparison in `ObjectDirectoryPath()` to require the prefix be followed by a path separator (or an exact match), e.g.:
```go
if absoluteObjectDirectoryPath != expectedQuarantinePrefix &&
    !strings.HasPrefix(absoluteObjectDirectoryPath, expectedQuarantinePrefix+string(os.PathSeparator)) {
    return "", structerr.NewInvalidArgument("not a valid relative path: %w", origError)
}
```
This mirrors the safe pattern already used in `internal/git/dirs.go` (`storageRoot+string(os.PathSeparator)`) and in `internal/gitaly/storage/storagemgr/partition/fsrecorder/fs.go` (`f.root+"/"`).

### Proof of Concept
Not independently reproduced in this analysis — no test harness or live cluster access was available (index-based review only). The conceptual PoC: given repository A with quarantine prefix `tmp/quarantine-<A-hash>` and a concurrently-existing sibling directory `tmp/quarantine-<A-hash>999/objects` created for repository B's push, a client request for repository A supplying `GitObjectDirectory = "../quarantine-<A-hash>999/objects"` (resolved relative to repo A's path) would pass the `strings.HasPrefix` check because it string-matches the unbounded prefix, without actually being a path under repository A's own quarantine directory.

### Citations

**File:** internal/git/localrepo/paths.go (L43-53)
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
```

**File:** internal/git/localrepo/paths.go (L61-73)
```go
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

**File:** internal/git/dirs.go (L26-29)
```go
func ObjectDirectories(ctx context.Context, logger log.Logger, storageRoot, repoPath string) ([]string, error) {
	objDir := filepath.Join(repoPath, "objects")
	return altObjectDirs(ctx, logger, storageRoot+string(os.PathSeparator), objDir, 0)
}
```

**File:** internal/git/dirs.go (L83-85)
```go
		if !strings.HasPrefix(newDir, storagePrefix) {
			return nil, alternateOutsideStorageError(newDir)
		}
```
