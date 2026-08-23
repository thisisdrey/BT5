### Title
Unsanitized tar extraction and SSRF in `CreateRepositoryFromSnapshot` allow storage escape / arbitrary file write - (File: `internal/gitaly/service/repository/create_repository_from_snapshot.go`)

### Summary
The `CreateRepositoryFromSnapshot` RPC downloads an archive from a caller-supplied `HttpUrl` and extracts it directly onto disk with the system `tar` binary, without any of the path/symlink-escape validation that Gitaly applies elsewhere for tar extraction. This mirrors the ERC4626 bug class in spirit: a critical value (the untrusted archive's path/entries) is used raw instead of being validated/sanitized first, letting attacker-controlled input corrupt state outside its intended boundary — here, files may be written outside the intended repository directory, and the HTTP fetch itself is an SSRF primitive.

### Finding Description
`untar()` builds an HTTP request straight from `in.GetHttpUrl()`, optionally attaches `in.GetHttpAuth()` as an `Authorization` header, and pipes the HTTP response body directly into `tar -C path -xvf -`: [1](#0-0) 

Unlike the RPC's sibling snapshot-extraction path in `replicate.go`, which manually parses the tar stream in Go and explicitly validates that every regular file, directory, hard link, and symlink target stays within `targetDir` (rejecting `..`-escaping symlinks/hardlinks and absolute symlinks): [2](#0-1) 

`untar()` performs no such validation — it relies entirely on the system `tar` binary's default behavior, which does not reject relative path traversal (`../`) in member names or symlink targets by default. The code even contains a comment acknowledging the archive is "trusted *a lot*" and needs hardening before being pointed at untrusted endpoints: [3](#0-2) 

Additionally, the caller fully controls the URL that Gitaly's HTTP client fetches (only mitigated if the caller also supplies a `ResolvedAddress`, which is optional), so the RPC itself is an SSRF primitive against Gitaly's network reachability.

### Impact Explanation
A crafted `HttpUrl` response (attacker-controlled archive) containing entries with `../` path components or malicious symlinks can escape the freshly created repository directory during extraction, potentially overwriting arbitrary files reachable by the Gitaly process (storage escape / arbitrary file write), analogous to how the ERC4626 bug let an unvalidated `amount` corrupt internal accounting outside its intended bound. Independently, `HttpUrl` being attacker-suppliable is an SSRF vector allowing internal network reconnaissance or credential exfiltration via `HttpAuth`. Since this operates within Gitaly's `repoutil.Create` repository-creation flow, exploitation directly threatens the on-disk storage layout that other repositories/pools depend on.

### Likelihood Explanation
`CreateRepositoryFromSnapshot` is a standard Gitaly RPC exposed like any other repository RPC (used for repository creation/migration/import flows), gated only by ordinary Gitaly RPC authentication — not an operator-only CLI tool. Any caller able to invoke it with a URL and archive under their control (e.g., during project import/migration) can trigger the unsanitized extraction path, making this a realistic, RPC-field-driven trigger rather than requiring a privileged actor or malicious peer node.

### Recommendation
Replace the raw `tar -C path -xvf -` invocation with the same in-Go, boundary-validated extraction logic already implemented in `extractTarToDirectory`/`extractFile` in `replicate.go` (validating file, hardlink, and symlink targets stay within `path`). Additionally, require and enforce `ResolvedAddress` (or otherwise restrict destination hosts) for every `CreateRepositoryFromSnapshot` call to eliminate the SSRF surface, rather than treating it as optional.

### Proof of Concept
1. Attacker controls (or can redirect, e.g., via URL bound to their own service) the HTTP endpoint referenced in `CreateRepositoryFromSnapshotRequest.HttpUrl`, omitting `ResolvedAddress`.
2. Server responds with a tar stream containing an entry such as `../../../../../etc/cron.d/evil` or a symlink entry pointing outside the target directory followed by a regular-file entry through that symlink.
3. Gitaly's `untar()` pipes this stream directly into system `tar -C <repoPath> -xvf -`; because no member-name or symlink-target validation is performed (unlike `extractTarToDirectory`), the malicious entries are written outside `repoPath`.
4. Separately, regardless of the tar payload, the outbound HTTP GET request itself demonstrates SSRF since `HttpUrl` is used unvalidated when `ResolvedAddress` is not supplied.

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

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L140-144)
```go
		// NOTE: The received archive is trusted *a lot*. Before pointing this RPC
		// at endpoints not under our control, it should undergo a lot of hardening.
		if err := s.untar(ctx, path, in); err != nil {
			return structerr.NewInternal("extracting snapshot: %w", err)
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
