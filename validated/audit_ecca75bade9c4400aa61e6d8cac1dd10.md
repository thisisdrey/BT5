### Title
Unvalidated tar extraction in `CreateRepositoryFromSnapshot` allows archive extraction escape - ([File: internal/gitaly/service/repository/create_repository_from_snapshot.go])

### Summary
The `evm` JUMPI advisory is a "check performed in the wrong place/order" bug class: a security-relevant validation (destination validity) was applied without properly gating on the condition that determines whether the action should happen at all, so an action proceeded on data that had not been properly vetted. The closest reachable Gitaly analog is `CreateRepositoryFromSnapshot`'s `untar` helper, which extracts an externally-fetched tar archive directly onto disk with **no path/name validation at all**, unlike the hardened extraction routine that exists elsewhere in the same codebase (`extractTarToDirectory` in `internal/gitaly/service/repository/replicate.go`), which does perform prefix/symlink/hardlink destination checks before acting on each entry.

### Finding Description
`CreateRepositoryFromSnapshot` accepts a `CreateRepositoryFromSnapshotRequest` containing an attacker/caller-controlled `HttpUrl`, optional `ResolvedAddress`, and `HttpAuth` header value. [1](#0-0) 

The handler fetches the URL and pipes the HTTP response body directly into a `tar -C path -xvf -` subprocess with no inspection of the archive's entry names: [2](#0-1) 

Compare this to the equivalent, hardened routine used by `replicate.go`'s `extractSnapshot`/`extractTarToDirectory`, which computes each entry's `targetPath`, and *before* performing any filesystem mutation (`MkdirAll`, file write, symlink, hardlink) verifies that `targetPath` (and, for symlinks/hardlinks, the resolved link target) stays within `targetDir`: [3](#0-2) 

In `untar`, this destination-containment check is entirely absent — the "is this write location valid" check that `extractTarToDirectory` performs before every mutating action is missing, so the shell-out to system `tar` performs the extraction unconditionally on attacker-influenced archive content. The code even carries a developer acknowledgment of this gap:
> "NOTE: The received archive is trusted *a lot*. Before pointing this RPC at endpoints not under our control, it should undergo a lot of hardening." [4](#0-3) 

This is structurally the same defect class as the JUMPI issue: the "is this operation permitted/safe" check is missing/deferred relative to where the action (destructive filesystem write) is actually performed, whereas the sibling implementation in the same codebase demonstrates the correct order (validate destination before acting).

### Impact Explanation
If an attacker can reach `CreateRepositoryFromSnapshot` (e.g., via direct gRPC access, a compromised/malicious Praefect-routed client, or a permitted internal caller pointed at attacker-controlled infrastructure via `HttpUrl`/`ResolvedAddress`), a crafted tar response containing `../` path components or symlink/hardlink entries pointing outside the target repository directory can cause files to be written or overwritten anywhere the Gitaly process has filesystem permissions — a classic "tar slip" archive extraction escape, resulting in storage escape and potential arbitrary file write outside the intended repository path.

### Likelihood Explanation
The RPC is unauthenticated with respect to archive contents — it fully trusts whatever the fetched URL returns and performs no server-side validation of the archive layout. Exploitation only requires the caller to control (or redirect, via `HttpUrl`) the HTTP response body of the fetch, which is a lower bar than exploiting a push/fetch hook path, and the code comment confirms the authors are aware this trust boundary is unenforced.

### Recommendation
Replace the direct `tar -C path -xvf -` invocation with the same validated, entry-by-entry extraction logic already implemented in `extractTarToDirectory` (path containment check prior to `MkdirAll`/file write/symlink/hardlink creation, rejection of absolute symlinks, and rejection of entries whose resolved target escapes `path`). Alternatively, invoke Go's `archive/tar` package directly and perform destination-path validation (`filepath.Join` + prefix check against the clean target directory) for every entry type before any filesystem mutation, matching the pattern in `replicate.go`.

### Proof of Concept
1. Stand up an HTTP server that responds to a GET request with a tar stream containing an entry named `../../../../tmp/evil` (or a symlink entry pointing outside the destination), instead of `HEAD`/`config`/`objects/...`.
2. Call `CreateRepositoryFromSnapshot` with `HttpUrl` pointing at that server (and, if required by deployment, `ResolvedAddress` set to bypass any DNS restriction).
3. Observe that `s.untar` shells out to `tar -C <repoPath> -xvf -` and extracts the crafted entry, writing/overwriting `/tmp/evil` (or another path outside `repoPath`), confirming the extraction escape since no per-entry destination validation exists in this code path (contrast with `TestExtractTarToDirectory_SymlinkValidation` in `replicate_test.go`, which validates the hardened sibling implementation but does not cover `untar`). [5](#0-4)

### Citations

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L87-121)
```go
func (s *server) untar(ctx context.Context, path string, in *gitalypb.CreateRepositoryFromSnapshotRequest) error {
	req, err := http.NewRequestWithContext(ctx, "GET", in.GetHttpUrl(), nil)
	if err != nil {
		return structerr.NewInvalidArgument("Bad HTTP URL: %w", err)
	}

	client := httpClient
	if resolvedAddress := in.GetResolvedAddress(); resolvedAddress != "" {
		client, err = newResolvedHTTPClient(in.GetHttpUrl(), resolvedAddress)
		if err != nil {
			return structerr.NewInvalidArgument("creating resolved HTTP client: %w", err)
		}
	}

	if in.GetHttpAuth() != "" {
		req.Header.Set("Authorization", in.GetHttpAuth())
	}

	rsp, err := client.Do(req)
	if err != nil {
		return structerr.NewInternal("HTTP request failed: %w", err)
	}
	defer rsp.Body.Close()

	if rsp.StatusCode < http.StatusOK || rsp.StatusCode >= http.StatusMultipleChoices {
		return structerr.NewInternal("HTTP server: %s", rsp.Status)
	}

	cmd, err := command.New(ctx, s.logger, []string{"tar", "-C", path, "-xvf", "-"}, command.WithStdin(rsp.Body))
	if err != nil {
		return err
	}

	return cmd.Wait()
}
```

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L139-144)
```go
		//
		// NOTE: The received archive is trusted *a lot*. Before pointing this RPC
		// at endpoints not under our control, it should undergo a lot of hardening.
		if err := s.untar(ctx, path, in); err != nil {
			return structerr.NewInternal("extracting snapshot: %w", err)
		}
```

**File:** internal/gitaly/service/repository/replicate.go (L314-404)
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

```

**File:** internal/gitaly/service/repository/replicate_test.go (L821-904)
```go
func TestExtractTarToDirectory_SymlinkValidation(t *testing.T) {
	t.Parallel()

	type tarEntry struct {
		header *tar.Header
		body   []byte
	}

	createTar := func(t *testing.T, entries []tarEntry) io.Reader {
		t.Helper()
		var buf bytes.Buffer
		tw := tar.NewWriter(&buf)
		for _, e := range entries {
			require.NoError(t, tw.WriteHeader(e.header))
			if len(e.body) > 0 {
				_, err := tw.Write(e.body)
				require.NoError(t, err)
			}
		}
		require.NoError(t, tw.Close())
		return &buf
	}

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

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()

			targetDir := t.TempDir()
			s := &server{logger: testhelper.NewLogger(t)}
			ctx := testhelper.Context(t)

			err := s.extractTarToDirectory(ctx, createTar(t, tc.entries), targetDir)
			if tc.expectError != "" {
				require.Error(t, err)
				require.Contains(t, err.Error(), tc.expectError)
			} else {
				require.NoError(t, err)
			}
		})
	}
}
```
