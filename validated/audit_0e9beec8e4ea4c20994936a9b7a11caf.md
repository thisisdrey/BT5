### Title
Uncontrolled path traversal / arbitrary file write via `tar` in `CreateRepositoryFromSnapshot` - (File: internal/gitaly/service/repository/create_repository_from_snapshot.go)

### Summary
`server.untar` streams an HTTP response body attacker-controllable via `HttpUrl`/`HttpAuth`/`ResolvedAddress` directly into the stdin of the external `tar -C path -xvf -` process with no per-entry path validation. Unlike the analogous `extractTarToDirectory` used by `ReplicateRepository`, which explicitly checks that every regular file, directory, symlink, and hardlink target resolves within `targetDir` [1](#0-0) , `CreateRepositoryFromSnapshot` relies entirely on the system `tar` binary's own (GNU tar-dependent) protections.

### Finding Description
`CreateRepositoryFromSnapshot` calls `s.untar(ctx, path, in)` inside `repoutil.Create`'s callback [2](#0-1) . `untar` builds an HTTP GET request from `in.GetHttpUrl()`, optionally uses `in.GetResolvedAddress()` to pin the DNS resolution and sets `Authorization` from `in.GetHttpAuth()`, then pipes the response body straight into `tar -C path -xvf -` via `command.New(... command.WithStdin(rsp.Body))` [3](#0-2) . There is no inspection of tar entry names for `../` sequences or absolute paths before invocation, and the code comment explicitly acknowledges this: "the received archive is trusted a lot... it should undergo a lot of hardening" [4](#0-3) .

By contrast, the sibling snapshot-extraction code path in `replicate.go` (used by `ReplicateRepository`) implements manual Go-native tar extraction (`extractTarToDirectory`) with explicit boundary checks for regular files, directories, symlinks (rejecting absolute symlink targets and verifying resolved targets stay under `targetDir`), and hardlinks [5](#0-4) . This confirms the Gitaly team is aware that unrestricted tar extraction is a path-escape risk, but that protection is absent from `CreateRepositoryFromSnapshot`.

Whether this is exploitable depends on modern GNU tar's default behavior: GNU tar has, since a long time, refused absolute paths and stripped/rejected `../` components by default (unless `--absolute-names`/insecure options are passed), which is why the comment frames the risk as "should undergo a lot of hardening" for *future* untrusted use rather than declaring a currently-confirmed escape. I could not verify the exact `tar` binary/version/flags used in the deployed container image within this repo, so I cannot confirm with certainty that the installed system `tar` lacks these default protections. However, the fundamental root cause remains: this handler performs *zero* application-level validation of tar entry paths, unlike `extractTarToDirectory`, and depends entirely on external/OS binary behavior and packaging choices that are outside Gitaly's control and could vary by OS/tar implementation or a future container base-image change.

### Impact Explanation
If the underlying `tar` binary does not enforce path confinement (e.g., older tar, bsdtar, or a future image change), an attacker able to supply `HttpUrl`/`HttpAuth` pointing at a server they control could serve a tar containing `../../../etc/cron.d/evil` or absolute-path entries, resulting in arbitrary file writes outside the newly created repository directory on the Gitaly host — a critical local file write / potential RCE impact class.

### Likelihood Explanation
Reaching this code requires the caller to be able to invoke `CreateRepositoryFromSnapshot` with attacker-influenced `HttpUrl`. In practice this RPC is not documented in this repo as being reachable directly by arbitrary authenticated GitLab users through Rails; it is an internal snapshot/import primitive typically invoked by trusted Rails/Gitaly-internal callers, not exposed as a generic tar-upload endpoint to end users. The question's precondition ("exposed to authenticated non-admin users in some GitLab flows, e.g. repository import") is asserted but not verifiable from the Gitaly repo alone — that authorization boundary lives in GitLab Rails, outside this codebase, and I found no code in this repo confirming unprivileged users can set `HttpUrl` to an arbitrary attacker-controlled endpoint for this specific RPC.

### Recommendation
Apply the same validation used in `extractTarToDirectory` (or invoke it) instead of shelling out to system `tar`, or add pre-extraction entry-name validation (reject absolute paths and `..` components, canonicalize and check `strings.HasPrefix(targetPath, targetDir+string(os.PathSeparator))`) before/while streaming to `tar`, and use `--no-same-owner`, avoid `--absolute-names`, and pass `-P`-free flags explicitly.

### Proof of Concept
Not conclusively reproducible from this repo alone: the outcome depends on the runtime `tar` binary's own path-sanitization behavior, which could not be verified in this environment. A concrete PoC would require running `tar -C <path> -xvf -` with a crafted archive containing `../evil` and `/etc/cron.d/evil` entries in the target deployment's OS to observe whether escape occurs; this determines whether the finding is exploitable in practice or merely a defense-in-depth gap already mitigated by the OS tar binary.

### Citations

**File:** internal/gitaly/service/repository/replicate.go (L315-406)
```go
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

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L88-120)
```go
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
```

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L129-144)
```go
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
```
