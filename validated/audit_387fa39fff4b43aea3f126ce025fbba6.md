### Title
Symlink entry in custom hooks tar bypasses repository isolation in `SetCustomHooks` - ([File: internal/gitaly/repoutil/custom_hooks.go])

### Summary
`SetCustomHooks` extracts an attacker-supplied tarball via `ExtractHooks`, which shells out to the system `tar` binary with no validation of entry types or link targets. A tar containing a top-level `custom_hooks` entry of type symlink is extracted literally as a symlink, and that symlink is later moved with `os.Rename(tempHooksPath, repoHooksPath)` directly onto the repository's hooks path, replacing the intended real directory with a symlink to an attacker-chosen filesystem path.

### Finding Description
`ExtractHooks` builds the tar extraction command purely from the untrusted stream with no per-entry validation: [1](#0-0) 

Unlike the hardened, hand-rolled extractor used elsewhere in the codebase for snapshot replication — which explicitly inspects `header.Typeflag == tar.TypeSymlink`, rejects absolute link targets, and verifies relative targets don't escape the extraction directory — `ExtractHooks` relies on the external `tar` command and applies none of these checks: [2](#0-1) 

Because of this, a crafted tar with a top-level entry named `custom_hooks` of `TypeSymlink` pointing to an absolute path (e.g. an existing readable file on the Gitaly host) is extracted as a real symlink at `tmpDir/custom_hooks`. `SetCustomHooks` subsequently does: [3](#0-2) 

`os.Mkdir` on the already-existing symlink path fails with `EEXIST`, which is explicitly ignored, so the symlink is left in place. The vote-generation walk `newDirectoryVote` uses `filepath.WalkDir`, whose root-level `os.Lstat` sees a symlink (`entry.IsDir()` is `false`), so it falls into the file-hashing branch and calls `os.Open`/`io.Copy` on the *dereferenced* target. This only succeeds if the symlink points to an existing, readable regular file (an existing directory target causes `io.Copy` to fail with `EISDIR`, aborting before any rename — this incidentally blocks directory-targeting attacks, but not file-targeting ones): [4](#0-3) 

When the vote succeeds, the code performs the final, unconditional move: [5](#0-4) 

`os.Rename` operates on the symlink itself (does not dereference it), so `repoHooksPath` — which is supposed to always be a real directory confined to the repository — becomes a symlink pointing to whatever absolute path the attacker chose in the tar's `Linkname` field. This is a genuine violation of the repository-isolation invariant: `custom_hooks` is no longer guaranteed to be a directory inside `repoPath`.

### Impact Explanation
Concrete impact is constrained by the same code path that enables it: the vote-hash step forces the symlink target to be an existing, readable *regular file* (not a directory), and hook execution / `GetCustomHooks` archiving do not dereference the symlink's contents (confirmed by `TestGetCustomHooks_symlink`, which shows the outbound tarball preserves the symlink entry rather than the target's content). As a result, this does not achieve arbitrary directory takeover or code execution through the hook mechanism (a subsequent `filepath.Join(repoHooksPath, "pre-receive")` against a symlink-to-file target simply resolves to an invalid, non-existent path). The concrete impact is corruption of the repository's `custom_hooks` entry into a dangling/misleading symlink pointing at an attacker-chosen absolute path on the Gitaly host, breaking the isolation invariant and silently disabling that repository's hook functionality — a storage-integrity/limited-DoS issue rather than cross-tenant file read or RCE.

### Likelihood Explanation
The attacker only needs the ability to call `SetCustomHooks` (or `RestoreCustomHooks`) with a controlled tar body on a repository they can push/administer — an unprivileged capability available to any project maintainer via GitLab Rails' hook-management endpoints. The only extra requirement is knowing an absolute path to an existing, readable regular file on the Gitaly node (e.g., a well-known system file), which is a reasonable assumption. This is deterministic and repeatable.

### Recommendation
Replace the external `tar` invocation in `ExtractHooks` (or add a pre/post-extraction validation pass) with logic that rejects or safely handles `TypeSymlink`/`TypeLink` entries, mirroring the checks already implemented in `extractTarToDirectory` in `internal/gitaly/service/repository/replicate.go` (reject absolute link targets, verify relative targets resolve within the extraction directory). Additionally, after extraction, verify `tempHooksPath` (and `repoHooksPath` prior to renaming into place) is a real directory via `os.Lstat` and abort if it is a symlink.

### Proof of Concept
```go
func TestSetCustomHooks_symlinkEscape(t *testing.T) {
    ctx := testhelper.Context(t)
    cfg := testcfg.Build(t)
    locator := config.NewLocator(cfg)
    txManager := transaction.NewTrackingManager()
    logger := testhelper.NewLogger(t)

    repo, repoPath := gittest.CreateRepository(t, ctx, cfg, gittest.CreateRepositoryConfig{
        SkipCreationViaService: true,
    })

    // Existing, readable regular file on the host that the attacker targets.
    target := "/etc/hostname"

    var buf bytes.Buffer
    tw := tar.NewWriter(&buf)
    require.NoError(t, tw.WriteHeader(&tar.Header{
        Name:     "custom_hooks",
        Typeflag: tar.TypeSymlink,
        Linkname: target,
    }))
    require.NoError(t, tw.Close())

    err := repoutil.SetCustomHooks(ctx, logger, locator, txManager, &buf, repo)
    require.NoError(t, err)

    hooksPath := filepath.Join(repoPath, "custom_hooks")
    info, err := os.Lstat(hooksPath)
    require.NoError(t, err)
    // Invariant violated: custom_hooks is a symlink, not a real directory.
    require.NotEqual(t, os.ModeDir, info.Mode()&os.ModeDir)
    require.Equal(t, os.ModeSymlink, info.Mode()&os.ModeSymlink)

    link, err := os.Readlink(hooksPath)
    require.NoError(t, err)
    require.Equal(t, target, link)
}
```
Expected: the assertions pass, demonstrating `repoHooksPath` ends up as a symlink to an attacker-chosen absolute path rather than a directory confined to `repoPath`.

### Citations

**File:** internal/gitaly/repoutil/custom_hooks.go (L69-77)
```go
	cmdArgs := []string{"-xf", "-", "-C", path, "--strip-components", stripComponents, CustomHooksDir}

	var stderrBuilder strings.Builder
	cmd, err := command.New(ctx, logger, append([]string{"tar"}, cmdArgs...),
		command.WithStdin(buf),
		command.WithStderr(&stderrBuilder))
	if err != nil {
		return fmt.Errorf("executing tar command: %w", err)
	}
```

**File:** internal/gitaly/repoutil/custom_hooks.go (L178-186)
```go
	tempHooksPath := filepath.Join(tmpDir.Path(), CustomHooksDir)

	// No hooks will be extracted if the tar archive is empty. If this happens
	// it means the repository should be set with an empty `custom_hooks`
	// directory. Create `custom_hooks` in the temporary directory so that any
	// existing repository hooks will be replaced with this empty directory.
	if err := os.Mkdir(tempHooksPath, mode.Directory); err != nil && !errors.Is(err, fs.ErrExist) {
		return fmt.Errorf("making temp hooks directory: %w", err)
	}
```

**File:** internal/gitaly/repoutil/custom_hooks.go (L220-223)
```go
	// Move `custom_hooks` from the temporary directory to the repository.
	if err := os.Rename(tempHooksPath, repoHooksPath); err != nil {
		return fmt.Errorf("moving new hooks to repo: %w", err)
	}
```

**File:** internal/gitaly/repoutil/custom_hooks.go (L257-298)
```go
func newDirectoryVote(basePath string) (*voting.VoteHash, error) {
	voteHash := voting.NewVoteHash()

	if err := filepath.WalkDir(basePath, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}

		relPath, err := filepath.Rel(basePath, path)
		if err != nil {
			return fmt.Errorf("getting relative path: %w", err)
		}

		// Write file relative path to hash.
		_, _ = voteHash.Write([]byte(relPath))

		info, err := entry.Info()
		if err != nil {
			return fmt.Errorf("getting file info: %w", err)
		}

		// Write file permissions to hash.
		permBytes := make([]byte, 4)
		binary.BigEndian.PutUint32(permBytes, uint32(info.Mode()))
		_, _ = voteHash.Write(permBytes)

		if entry.IsDir() {
			return nil
		}

		file, err := os.Open(path)
		if err != nil {
			return fmt.Errorf("opening file: %w", err)
		}
		defer file.Close()

		// Copy file data to hash.
		if _, err = io.Copy(voteHash, file); err != nil {
			return fmt.Errorf("copying file to hash: %w", err)
		}

		return nil
```

**File:** internal/gitaly/service/repository/replicate.go (L352-373)
```go
		case tar.TypeSymlink:
			if filepath.IsAbs(header.Linkname) {
				return fmt.Errorf("absolute symlink not allowed: %s -> %s", header.Name, header.Linkname)
			}

			// Resolve the relative symlink target from the symlink's parent directory
			// and verify it stays within the extraction boundary, consistent with the
			// hard link validation below.
			resolvedTarget := filepath.Join(filepath.Dir(targetPath), header.Linkname)
			if !strings.HasPrefix(resolvedTarget, targetDir+string(os.PathSeparator)) &&
				resolvedTarget != targetDir {
				return fmt.Errorf("symlink target escapes extraction directory: %s -> %s", header.Name, header.Linkname)
			}

			// Remove existing file/symlink if it exists
			if err := os.Remove(targetPath); err != nil && !os.IsNotExist(err) {
				return fmt.Errorf("removing existing file for symlink %s: %w", targetPath, err)
			}

			if err := os.Symlink(header.Linkname, targetPath); err != nil {
				return fmt.Errorf("creating symlink %s -> %s: %w", targetPath, header.Linkname, err)
			}
```
