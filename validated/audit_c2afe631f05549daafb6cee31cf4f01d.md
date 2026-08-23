### Title
Symlink-pivot path escape in snapshot tar extraction bypasses directory-boundary check - (File: internal/gitaly/service/repository/replicate.go)

### Summary
### Finding Description
`ReplicateRepository`'s `createFromSnapshot` path pulls a tar stream from a source Gitaly node via `GetSnapshot` and extracts it locally with `extractTarToDirectory` / `extractFile`. [1](#0-0) 

The extractor validates each tar entry's *computed string path* against the target directory prefix before writing, for regular files, symlinks, and hard links: [2](#0-1) 

This mirrors the H-1 bug class: a security-relevant invariant ("the effective write always stays inside `targetDir`") is checked against a *pre-computed* value (`filepath.Join(targetDir, header.Name)` as a string) rather than against the *actual state of the filesystem at the moment the operation executes* (`os.OpenFile`/`os.MkdirAll`). Just as the Portal checked `gasleft()` at one point in time but consumed the call with a different (lower) gas value moments later, `extractTarToDirectory` checks a string path at validation time but performs `os.MkdirAll(filepath.Dir(targetPath), ...)` and `os.OpenFile(targetPath, ...)` afterwards, without re-resolving the path through the live directory tree with `os.Lstat`/`filepath.EvalSymlinks`.

Because tar entries are processed sequentially and attacker-influenced (an attacker who controls the *source* repository content that gets streamed by `GetSnapshot`, e.g., a compromised/malicious source node in a Gitaly cluster, or any structure that ends up inside the repository directory that gets included in the snapshot tar), an attacker can:
1. Emit a `TypeSymlink` entry for `foo` whose target is a relative path validated to stay inside `targetDir` (e.g. `foo -> subdir`), passing the individual-entry check at `internal/gitaly/service/repository/replicate.go:357-364`.
2. Emit a later `TypeReg` entry named `foo/evil.txt`. `extractFile` computes `targetPath = filepath.Join(targetDir, "foo/evil.txt")`, which is a *textually* valid path inside `targetDir`, so it passes the `strings.HasPrefix` gate — but at write time the OS resolves `foo` through the symlink created in step 1, and any directory the symlink chain ultimately resolves through (which is not re-validated) is where the write actually lands. Because Gitaly only string-checks the immediate symlink's declared target, not the fully resolved path used at write time, any component of the path that was replaced by a symlink between validation and execution is not re-checked, exactly the "gap between check and execution" pattern from the report.

Existing tests only cover `..`-escaping symlinks and absolute-path symlinks, not the pivot-via-nested-write scenario: [3](#0-2) 

### Impact Explanation
If exploitable end-to-end, this allows a malicious/compromised replication source to make the destination Gitaly node write attacker-controlled file content to arbitrary paths reachable through a symlinked directory component during `ReplicateRepository`/snapshot-based repository creation, i.e. a storage-boundary escape during archive extraction — one of the explicitly in-scope analog classes (archive/bundle extraction escape). This could corrupt or plant files outside the intended repository directory on the destination storage.

### Likelihood Explanation
Exploitability depends on whether `GetSnapshot` (the tar-producing side) can be induced to include a symlink entry that itself is a legitimate repository artifact (the codebase shows Gitaly repositories are expected NOT to contain symlinks, and other code paths reject repositories containing symlinks during snapshot creation) — I could not fully confirm within the remaining budget whether `GetSnapshot`'s tar-writing side ever emits `TypeSymlink` entries for ordinary repository content or only regular files/directories. If snapshot creation strictly refuses symlinks (as suggested by `CreateSnapshot`'s "too many levels of symbolic links" behavior), the write-path here would only be reachable via a malicious/compromised replication peer producing a crafted tar stream directly (bypassing `GetSnapshot`'s own construction), which narrows likelihood. This is flagged as **unconfirmed** — the check-vs-execution gap in `extractTarToDirectory`/`extractFile` is real and matches the reported bug class, but full proof of an end-to-end reachable path from an ordinary user action (push/fetch/fork) was not completed.

### Recommendation
In `extractTarToDirectory`/`extractFile` (`internal/gitaly/service/repository/replicate.go`), re-validate the fully resolved path immediately before each filesystem write rather than relying solely on the string-computed `targetPath`/`resolvedTarget`. Concretely:
- Before creating any file/dir/hardlink, `os.Lstat` each existing intermediate path component (or use `filepath.EvalSymlinks` on `filepath.Dir(targetPath)`) and confirm the resolved real path is still within `targetDir` at the time of the write, not just at validation time.
- Reject any tar entry whose parent directory component was created as a symlink earlier in the same extraction, or refuse to follow symlinks created during the same extraction pass entirely (extract non-symlink content only, then create symlinks last, after verifying no other future entry references them as an intermediate path).
- Consider disallowing symlink entries in snapshot tar streams altogether, consistent with the existing invariant elsewhere in the codebase that Gitaly repositories should not contain symlinks.

### Proof of Concept
Not independently reproduced end-to-end against the live RPC due to tool-call budget; the unit test `TestExtractTarToDirectory_SymlinkValidation` at `internal/gitaly/service/repository/replicate_test.go` confirms the extraction logic and its current coverage (`..`-escape and absolute-symlink rejection), but does not include a test for the "symlink then nested write" pivot sequence described above — a background agent with filesystem/terminal access should construct a tar stream with entries `[dir "d/", symlink "d/link" -> "d" (valid, self-contained), regular file "d/link/evil.txt"]` (and variations chaining through multiple pre-existing directories) fed directly into `extractTarToDirectory` to confirm whether the final write escapes `targetDir`, and separately trace `GetSnapshot`'s tar-writing implementation to determine whether it can be made to emit attacker-influenced symlink entries.

### Citations

**File:** internal/gitaly/service/repository/replicate.go (L264-312)
```go
func (s *server) extractSnapshot(ctx context.Context, source, target *gitalypb.Repository) error {
	repoClient, err := s.newRepoClient(ctx, source.GetStorageName())
	if err != nil {
		return fmt.Errorf("new client: %w", err)
	}

	stream, err := repoClient.GetSnapshot(ctx, &gitalypb.GetSnapshotRequest{Repository: source})
	if err != nil {
		return fmt.Errorf("get snapshot: %w", err)
	}

	// We need to catch a possible 'invalid repository' error from GetSnapshot. On an empty read,
	// we read the first message from the stream here to get access to the possible 'invalid repository' error.
	firstBytes, err := stream.Recv()
	if err != nil {
		switch {
		case structerr.GRPCCode(err) == codes.NotFound && strings.Contains(err.Error(), "GetRepoPath: not a git repository:"):
			// The error condition exists for backwards compatibility purposes, only,
			// and can be removed in the next release.
			return ErrInvalidSourceRepository
		case structerr.GRPCCode(err) == codes.NotFound && strings.Contains(err.Error(), storage.ErrRepositoryNotFound.Error()):
			return ErrInvalidSourceRepository
		case structerr.GRPCCode(err) == codes.FailedPrecondition && strings.Contains(err.Error(), storage.ErrRepositoryNotValid.Error()):
			return ErrInvalidSourceRepository
		default:
			return fmt.Errorf("first snapshot read: %w", err)
		}
	}

	snapshotReader := io.MultiReader(
		bytes.NewReader(firstBytes.GetData()),
		streamio.NewReader(func() ([]byte, error) {
			resp, err := stream.Recv()
			return resp.GetData(), err
		}),
	)

	targetPath, err := s.locator.GetRepoPath(ctx, target, storage.WithRepositoryVerificationSkipped())
	if err != nil {
		return fmt.Errorf("target path: %w", err)
	}

	// Extract tar using Go's tar package
	if err := s.extractTarToDirectory(ctx, snapshotReader, targetPath); err != nil {
		return fmt.Errorf("extract tar: %w", err)
	}

	return nil
}
```

**File:** internal/gitaly/service/repository/replicate.go (L314-406)
```go
// extractTarToDirectory extracts a tar archive to the specified directory using Go's tar package
func (s *server) extractTarToDirectory(ctx context.Context, reader io.Reader, targetDir string) error {
	targetDir = filepath.Clean(targetDir)
	tarReader := tar.NewReader(reader)

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}

		header, err := tarReader.Next()
		if err == io.EOF {
			break // End of archive
		}
		if err != nil {
			return fmt.Errorf("reading tar header: %w", err)
		}

		targetPath := filepath.Join(targetDir, header.Name)

		if !strings.HasPrefix(targetPath, targetDir+string(os.PathSeparator)) &&
			targetPath != targetDir {
			return fmt.Errorf("invalid file path in tar: %s", header.Name)
		}

		switch header.Typeflag {
		case tar.TypeDir:
			if err := os.MkdirAll(targetPath, os.FileMode(header.Mode)); err != nil {
				return fmt.Errorf("creating directory %s: %w", targetPath, err)
			}

		case tar.TypeReg:
			if err := s.extractFile(ctx, tarReader, targetPath, header); err != nil {
				return fmt.Errorf("extracting file %s: %w", targetPath, err)
			}

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

		case tar.TypeLink:
			linkTarget := filepath.Join(targetDir, header.Linkname)

			if !strings.HasPrefix(linkTarget, targetDir+string(os.PathSeparator)) &&
				linkTarget != targetDir {
				return fmt.Errorf("invalid hard link target: %s", header.Linkname)
			}

			// Remove existing file if it exists
			if err := os.Remove(targetPath); err != nil && !os.IsNotExist(err) {
				return fmt.Errorf("removing existing file for hard link %s: %w", targetPath, err)
			}

			if err := os.Link(linkTarget, targetPath); err != nil {
				return fmt.Errorf("creating hard link %s -> %s: %w", targetPath, linkTarget, err)
			}

		default:
			// Skip unsupported file types (devices, FIFOs, etc.)
			s.logger.WithField("file", header.Name).WithField("type", header.Typeflag).
				WarnContext(ctx, "skipping unsupported file type in tar archive")
		}

		if header.Typeflag == tar.TypeReg || header.Typeflag == tar.TypeDir {
			if err := os.Chmod(targetPath, os.FileMode(header.Mode)); err != nil {
				return fmt.Errorf("setting permissions for %s: %w", targetPath, err)
			}
		}
	}

	return nil
}
```

**File:** internal/gitaly/service/repository/replicate_test.go (L844-885)
```go
	tests := []struct {
		name        string
		entries     []tarEntry
		expectError string
	}{
		{
			name: "relative symlink within directory is allowed",
			entries: []tarEntry{
				{header: &tar.Header{Name: "subdir/", Typeflag: tar.TypeDir, Mode: 0o755}},
				{header: &tar.Header{Name: "subdir/target.txt", Typeflag: tar.TypeReg, Mode: 0o644, Size: 5}, body: []byte("hello")},
				{header: &tar.Header{Name: "link", Typeflag: tar.TypeSymlink, Linkname: "subdir/target.txt"}},
			},
		},
		{
			name: "relative symlink escaping via dotdot is rejected",
			entries: []tarEntry{
				{header: &tar.Header{Name: "valid_before.txt", Typeflag: tar.TypeReg, Mode: 0o644, Size: 3}, body: []byte("aaa")},
				{header: &tar.Header{Name: "escape", Typeflag: tar.TypeSymlink, Linkname: "../../../../tmp"}},
				{header: &tar.Header{Name: "valid_after.txt", Typeflag: tar.TypeReg, Mode: 0o644, Size: 3}, body: []byte("bbb")},
			},
			expectError: "symlink target escapes extraction directory",
		},
		{
			name: "absolute symlink is still rejected",
			entries: []tarEntry{
				{header: &tar.Header{Name: "valid_before.txt", Typeflag: tar.TypeReg, Mode: 0o644, Size: 3}, body: []byte("aaa")},
				{header: &tar.Header{Name: "abs", Typeflag: tar.TypeSymlink, Linkname: "/etc/passwd"}},
				{header: &tar.Header{Name: "valid_after.txt", Typeflag: tar.TypeReg, Mode: 0o644, Size: 3}, body: []byte("bbb")},
			},
			expectError: "absolute symlink not allowed",
		},
		{
			name: "nested relative symlink escaping is rejected",
			entries: []tarEntry{
				{header: &tar.Header{Name: "a/b/", Typeflag: tar.TypeDir, Mode: 0o755}},
				{header: &tar.Header{Name: "a/b/safe.txt", Typeflag: tar.TypeReg, Mode: 0o644, Size: 3}, body: []byte("aaa")},
				{header: &tar.Header{Name: "a/b/link", Typeflag: tar.TypeSymlink, Linkname: "../../../etc"}},
				{header: &tar.Header{Name: "a/b/another.txt", Typeflag: tar.TypeReg, Mode: 0o644, Size: 3}, body: []byte("bbb")},
			},
			expectError: "symlink target escapes extraction directory",
		},
	}
```
