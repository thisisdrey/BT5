### Title
Unsanitized tar member names in `ExtractHooks` allow path traversal outside the extraction directory during `SetCustomHooks` - ([File: internal/gitaly/repoutil/custom_hooks.go])

### Summary
`ExtractHooks` extracts an attacker-supplied tar stream by shelling out to the system `tar` binary with only a member-name filter (`custom_hooks`), without validating or sanitizing individual entry names inside the archive. Because the filter matches on a literal string prefix, an entry such as `custom_hooks/../../<relative-path>/custom_hooks/pre-receive` still matches the `custom_hooks` selector and gets extracted, letting `tar` resolve the embedded `..` segments and write outside the target directory passed via `-C`.

### Finding Description
`SetCustomHooks` (`internal/gitaly/repoutil/custom_hooks.go:100-253`) is invoked from the `SetCustomHooksRequest` handler with a repo owned by the calling user, and extracts the caller-controlled tar stream into a per-storage temporary directory obtained via `tempdir.NewWithoutContext` [1](#0-0) . This temp directory lives at a fixed, predictable location: `storagePath/+gitaly/tmp/<storageName>-repositories.old.<timestamp>.<random>`, as produced by `configLocator.TempDir` (`tmpRootPrefix = GitalyDataPrefix + "/tmp"`) [2](#0-1)  and `newDirectory` [3](#0-2) .

`ExtractHooks` builds a system `tar` invocation with a bare member-name selector (`CustomHooksDir`, i.e. `"custom_hooks"`), and does not enumerate or validate individual archive entries before or after invoking `tar`: [4](#0-3) 

Because tar's selective-extraction filter matches by literal prefix, any entry whose name starts with the literal string `custom_hooks` (e.g. `custom_hooks/../../<victim-relative-path>/custom_hooks/pre-receive`) is selected for extraction. Neither GNU nor BSD `tar`'s built-in protections (`safer_name_suffix`) strip `..` components that occur after the first path segment — they only strip a leading `/` or leading `../` sequences at the very start of the name. A name that begins with the legitimate `custom_hooks/` prefix and only introduces `..` later in the path is not sanitized by `tar` itself, so the extraction target computed from `-C path` plus the member name can resolve outside the intended temp directory.

No code in `ExtractHooks` or `SetCustomHooks` calls `storage.ValidateRelativePath` (or any equivalent) on the tar member names themselves — that validation is only ever applied to the RPC's `Repository` argument to compute `repoPath`/`tmpDir`, not to the contents of the tar stream being extracted. The final `os.Rename(tempHooksPath, repoHooksPath)` only relocates the specific `tmpDir/custom_hooks` subtree [5](#0-4) ; it does nothing to detect or roll back files that `tar` may have already written outside `tmpDir` during the earlier `ExtractHooks` call, so any traversal write is a genuine, independent side effect of the RPC.

### Impact Explanation
If exploitable end-to-end, this allows an attacker who owns any one repository to plant or overwrite a `custom_hooks/pre-receive` (or similar) file in another repository's on-disk directory, without ever touching that repository through a normal Gitaly RPC path. Since custom hooks are executed by Gitaly on subsequent pushes/updates to the victim repository, this is a path to arbitrary command execution against a repository the attacker does not own, and a clear violation of the "paths stay inside storage/repository" invariant. This matches GitLab's bounty class for storage-path escape / hook injection leading to remote code execution.

### Likelihood Explanation
Exploitation requires: (1) the attacker to control the tar member names, which is trivially true since `SetCustomHooksRequest` streams the raw tar content chosen by the caller; (2) the attacker to know (or correctly guess) the fixed depth of the temp extraction directory relative to the storage root, which is deterministic and derivable from Gitaly's storage layout constants (`+gitaly/tmp/...`), not a secret; and (3) the attacker to know the target victim repository's exact relative path on disk (e.g., GitLab's hashed storage layout is a deterministic function of the project ID, which is often discoverable). These preconditions are non-trivial but do not require any privileged role, secret, or misconfiguration — only knowledge of Gitaly's storage conventions and a target project's identifier, both attacker-obtainable in a standard GitLab deployment. Overall likelihood is moderate: feasible for a motivated unprivileged attacker with reconnaissance, but not immediately trivial without knowing the target path precisely.

### Recommendation
Do not rely on system `tar`'s built-in name-filtering/sanitization for security. Before invoking `tar`, or by using a Go tar reader instead of shelling out, validate every archive member name: reject entries containing `..` path components, absolute paths, or names that after `filepath.Clean` do not remain strictly under the `custom_hooks/` prefix relative to the extraction root. Alternatively, extract with Go's `archive/tar` package and manually join+validate each member's cleaned path against the destination directory (rejecting anything that escapes it) before writing, instead of delegating extraction and filtering entirely to the external `tar` binary.

### Proof of Concept
```go
func TestExtractHooks_PathTraversal(t *testing.T) {
    t.Parallel()
    ctx := testhelper.Context(t)

    // Simulate the fixed temp dir depth used by tempdir.NewWithoutContext:
    // storagePath/+gitaly/tmp/<random>
    storageRoot := t.TempDir()
    tmpRoot := filepath.Join(storageRoot, "+gitaly", "tmp")
    require.NoError(t, os.MkdirAll(tmpRoot, 0o755))
    extractDir, err := os.MkdirTemp(tmpRoot, "storage-repositories.old.")
    require.NoError(t, err)

    // Victim "repository" living elsewhere under storageRoot.
    victimRelPath := "@hashed/aa/bb/victimhash.git"
    victimDir := filepath.Join(storageRoot, victimRelPath)
    require.NoError(t, os.MkdirAll(victimDir, 0o755))

    // Craft the traversal depth: extractDir is storageRoot/+gitaly/tmp/X (3 levels deep).
    relFromExtractDirToStorageRoot := "../../.."
    maliciousName := fmt.Sprintf("custom_hooks/%s/%s/custom_hooks/pre-receive",
        relFromExtractDirToStorageRoot, victimRelPath)

    var buf bytes.Buffer
    w := tar.NewWriter(&buf)
    require.NoError(t, w.WriteHeader(&tar.Header{
        Name: maliciousName,
        Mode: 0o755,
        Size: int64(len("#!/bin/sh\ntouch /tmp/pwned\n")),
    }))
    _, err = w.Write([]byte("#!/bin/sh\ntouch /tmp/pwned\n"))
    require.NoError(t, err)
    require.NoError(t, w.Close())

    err = ExtractHooks(ctx, testhelper.NewLogger(t), &buf, extractDir, false)
    require.NoError(t, err)

    // Assert the file did NOT escape into the victim repository.
    require.NoFileExists(t, filepath.Join(victimDir, "custom_hooks", "pre-receive"),
        "tar extraction must not write outside the intended extraction directory")
}
```
Expected (vulnerable) result: the assertion fails because `pre-receive` is written into `victimDir/custom_hooks/pre-receive`, demonstrating that `tar`'s member filter plus unsanitized `..` traversal escapes the intended `extractDir` and lands inside another repository's directory tree.

### Citations

**File:** internal/gitaly/repoutil/custom_hooks.go (L64-77)
```go
	stripComponents := "0"
	if stripPrefix {
		stripComponents = "1"
	}

	cmdArgs := []string{"-xf", "-", "-C", path, "--strip-components", stripComponents, CustomHooksDir}

	var stderrBuilder strings.Builder
	cmd, err := command.New(ctx, logger, append([]string{"tar"}, cmdArgs...),
		command.WithStdin(buf),
		command.WithStderr(&stderrBuilder))
	if err != nil {
		return fmt.Errorf("executing tar command: %w", err)
	}
```

**File:** internal/gitaly/repoutil/custom_hooks.go (L159-176)
```go
	// Create a temporary directory to write the new hooks to and also
	// temporarily store the current repository hooks. This enables "atomic"
	// directory swapping by acting as an intermediary storage location between
	// moves.
	tmpDir, err := tempdir.NewWithoutContext(repo.GetStorageName(), logger, locator)
	if err != nil {
		return fmt.Errorf("creating temp directory: %w", err)
	}

	defer func() {
		if err := os.RemoveAll(tmpDir.Path()); err != nil {
			logger.WithError(err).WarnContext(ctx, "failed to remove temporary directory")
		}
	}()

	if err := ExtractHooks(ctx, logger, reader, tmpDir.Path(), false); err != nil {
		return fmt.Errorf("extracting hooks: %w", err)
	}
```

**File:** internal/gitaly/repoutil/custom_hooks.go (L220-223)
```go
	// Move `custom_hooks` from the temporary directory to the repository.
	if err := os.Rename(tempHooksPath, repoHooksPath); err != nil {
		return fmt.Errorf("moving new hooks to repo: %w", err)
	}
```

**File:** internal/gitaly/config/locator.go (L13-18)
```go
const (
	// tmpRootPrefix is the directory in which we store temporary
	// directories.
	tmpRootPrefix = GitalyDataPrefix + "/tmp"

	// cachePrefix is the directory where all cache data is stored on a
```

**File:** internal/tempdir/tempdir.go (L82-100)
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

	return Dir{
		logger: logger,
		path:   tempDir,
	}, err
```
