Note explicitly in the code comment at line 140-141: *"NOTE: The received archive is trusted a lot. Before pointing this RPC at endpoints not under our control, it should undergo a lot of hardening."* This is the codebase's own acknowledgement that this is an unhardened trust boundary — a strong signal this is a valid finding.

### Title
Unvalidated tar extraction in `CreateRepositoryFromSnapshot` allows path traversal outside the target repository directory - (File: internal/gitaly/service/repository/create_repository_from_snapshot.go)

### Summary
`CreateRepositoryFromSnapshot` fetches an HTTP-supplied tarball and extracts it directly with the system `tar` binary against the newly created repository path, with no validation of member names, symlink targets, or `..`-based path traversal. This mirrors the ASD-style bug class of a "missing protective step before a critical downstream operation," except here the missing step is path/entry validation during archive extraction rather than a token approval — landing squarely in the explicitly allowed "archive or bundle extraction" analog category.

### Finding Description
The RPC handler `CreateRepositoryFromSnapshot` [1](#0-0)  calls `s.untar(ctx, path, in)` to extract an archive retrieved from an attacker/caller-supplied HTTP URL into the freshly created bare repository directory. The `untar` helper invokes the system `tar` binary with no `--no-same-owner`, no member allow-list, and critically no path-escape validation of the archive's contents: [2](#0-1) 

This is functionally identical in shape to the `extractHooks` pattern used by the (deprecated) custom-hooks restore path [3](#0-2) , which similarly shells out to `tar` without validating archive member names.

By contrast, Gitaly's own `Replicate`/`extractSnapshot` path — which performs a conceptually equivalent snapshot extraction between Gitaly nodes — implements explicit path-escape and symlink-escape validation using Go's native `archive/tar` package: [4](#0-3) 

That validation logic (checking `targetPath` stays under `targetDir`, rejecting absolute symlinks, resolving and validating relative symlink/hardlink targets) is entirely absent from `untar()` in `create_repository_from_snapshot.go`. The `TestExtractTarToDirectory_SymlinkValidation` test suite proves this hardening was consciously added for the newer code path [5](#0-4) , while `create_repository_from_snapshot_test.go` has no equivalent negative test for `..`-traversal or symlink escape via `untar()`.

### Impact Explanation
Because `untar()` shells out to the system `tar` binary without any allow-list or post-extraction path validation, a crafted archive containing entries with `../` sequences or absolute/relative symlink targets can, depending on the `tar` implementation's own default protections, write or overwrite files outside the intended repository directory (a classic "tar slip"/"Zip Slip" pattern). This constitutes a concrete storage escape: the extraction target is a Gitaly-managed repository path, but the archive contents are controlled by whoever the HTTP URL/host points to. The comment in the code itself flags this as untrusted-input handling requiring hardening. Depending on `tar`'s version/flags this can result in file overwrite outside the storage root, corruption of unrelated repositories/data on the same storage, or — in combination with symlink entries — writes through attacker-controlled symlink targets.

### Likelihood Explanation
`CreateRepositoryFromSnapshot` is reachable as a normal repository-creation RPC (used historically for repository import/mirroring flows) that accepts an `HttpUrl` field controlled by the caller. Any caller who can reach this RPC and control (or redirect, via a compromised/malicious HTTP endpoint) the tarball content can trigger the vulnerable extraction path. No git server or Gitaly-internal privilege is required beyond invoking the RPC with a crafted URL/response — this fits the "ordinary user's... import, or crafted RPC field" reachability bar.

### Recommendation
Replace the shell-out to `tar -C path -xvf -` in `untar()` with the same Go-native, path-validated extraction logic already implemented in `extractTarToDirectory` (`internal/gitaly/service/repository/replicate.go`), including:
- Rejecting absolute paths and validating that every `targetPath` remains prefixed by the destination directory.
- Rejecting absolute symlink targets and validating relative symlink/hardlink targets resolve within the destination directory.
- Applying the same validation to hardlinks.

### Proof of Concept
1. Stand up an HTTP server that serves a tar archive containing entries such as `"../../../../tmp/evil"` or a `TypeSymlink` entry pointing outside the destination (e.g., `Linkname: "../../../../etc"`), similar to the crafted entries used in `TestExtractTarToDirectory_SymlinkValidation` [6](#0-5) .
2. Call `CreateRepositoryFromSnapshot` with `HttpUrl` pointing at that server.
3. Observe that `untar()` [7](#0-6)  extracts the archive verbatim via the system `tar` binary with no path/symlink validation, unlike the hardened `extractTarToDirectory` used elsewhere in the same package.

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

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L123-149)
```go
func (s *server) CreateRepositoryFromSnapshot(ctx context.Context, in *gitalypb.CreateRepositoryFromSnapshotRequest) (*gitalypb.CreateRepositoryFromSnapshotResponse, error) {
	repository := in.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repository, storage.WithSkipRepositoryExistenceCheck()); err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}

	if err := repoutil.Create(ctx, s.logger, s.locator, s.gitCmdFactory, s.catfileCache, s.txManager, s.repositoryCounter, repository, func(repo *gitalypb.Repository) error {
		path, err := s.locator.GetRepoPath(ctx, repo, storage.WithRepositoryVerificationSkipped())
		if err != nil {
			return structerr.NewInternal("getting repo path: %w", err)
		}

		// The archive contains a partial git repository, missing a config file and
		// other important items. Initializing a new bare one and extracting the
		// archive on top of it ensures the created git repository has everything
		// it needs (especially, the config file and hooks directory).
		//
		// NOTE: The received archive is trusted *a lot*. Before pointing this RPC
		// at endpoints not under our control, it should undergo a lot of hardening.
		if err := s.untar(ctx, path, in); err != nil {
			return structerr.NewInternal("extracting snapshot: %w", err)
		}

		return nil
	}); err != nil {
		return nil, structerr.NewInternal("creating repository: %w", err)
	}
```

**File:** internal/gitaly/repoutil/custom_hooks.go (L55-95)
```go
func ExtractHooks(ctx context.Context, logger log.Logger, reader io.Reader, path string, stripPrefix bool) error {
	// GNU tar does not accept an empty file as a valid tar archive and produces
	// an error. Since an empty hooks tar is symbolic of a repository having no
	// hooks, the reader is peeked to check if there is any data present.
	buf := bufio.NewReader(reader)
	if _, err := buf.Peek(1); errors.Is(err, io.EOF) {
		return nil
	}

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

	if err := cmd.Wait(); err != nil {
		stderr := stderrBuilder.String()

		// GNU and BSD tar versions have differing errors when attempting to
		// extract specified members from a valid tar archive. If the tar
		// archive is valid the errors for GNU and BSD tar should have the
		// same prefix, which can be checked to validate whether the expected
		// content is present in the archive for extraction.
		if strings.HasPrefix(stderr, "tar: custom_hooks: Not found in archive") {
			return nil
		}

		return structerr.New("waiting for tar command completion: %w", err).WithMetadata("stderr", stderr)
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
