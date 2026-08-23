### Title
Unvalidated tar extraction in `CreateRepositoryFromSnapshot` allows path traversal / arbitrary file write outside the target repository - (File: `internal/gitaly/service/repository/create_repository_from_snapshot.go`)

### Summary
The external report's core issue is that `FeeDistributorFactory` accepts *any* caller-supplied implementation of `feeDistributor` without validating/whitelisting it, letting an attacker substitute a malicious implementation that the system otherwise fully trusts. The analogous pattern in Gitaly is `CreateRepositoryFromSnapshot`, which fetches an archive from an attacker-controlled URL and extracts it directly onto disk with a bare `tar -xvf` invocation, without any of the path-traversal/symlink-escape validation that Gitaly's own maintainers later added to the equivalent snapshot-extraction code path (`internal/gitaly/service/repository/replicate.go`). The content of the archive is "trusted a lot" — the code's own comment says so — mirroring the missing-whitelist trust problem in the report.

### Finding Description
`CreateRepositoryFromSnapshot` lets a caller supply an arbitrary `HttpUrl` (optionally with `HttpAuth` and a `ResolvedAddress` used only to pin DNS resolution, not to constrain destination) that Gitaly's `untar` helper fetches and pipes straight into `tar -C path -xvf -`: [1](#0-0) 

Unlike the tar-extraction logic that was later written for repository replication, this path performs **no validation whatsoever** of the tar entries: it does not check that regular-file, directory, symlink, or hard-link target paths stay within the destination directory. The code's own comment acknowledges this: [2](#0-1) 

Compare this to the hardened, explicit checks Gitaly added for the analogous `extractTarToDirectory` used during repository replication, which validates that regular files, symlink targets, and hard-link targets do not escape `targetDir`: [3](#0-2) 

`CreateRepositoryFromSnapshot`'s `untar` has none of these protections — it delegates entirely to the system `tar` binary's own (version-dependent, not guaranteed) protections against `../` traversal or `TypeSymlink`/`TypeLink` escapes, with zero application-level enforcement. Just as the report's `FeeDistributorFactory` trusted any `feeDistributor` bytecode supplied by the caller without a whitelist, this RPC trusts any archive content fetched from an attacker-supplied URL without verifying that the paths it contains are confined to the newly created repository.

### Impact Explanation
If the fetched archive (from a URL fully controlled by the RPC caller, which could be an attacker-controlled or compromised HTTP endpoint) contains a tar entry with a `../` relative path, an absolute path, or a malicious symlink, `tar -xvf` may write or overwrite files outside the intended repository directory on the Gitaly node — e.g., overwriting other repositories' Git objects/config/hooks within the same storage, or (depending on the tar implementation's own protections) more broadly on the filesystem. This can lead to storage escape / cross-repository object or config corruption, and in the worst case remote code execution if a hook file or binary consumed elsewhere on the host is overwritten.

### Likelihood Explanation
The `HttpUrl` field is fully attacker-controlled input to the RPC; `CreateRepositoryFromSnapshot` is a `MUTATOR` RPC reachable by any caller permitted to create repositories (e.g., during repository import/migration flows), and no additional privileged access is required beyond what's already needed to invoke repository-creation RPCs. The extraction logic performs no path sanitization at the Gitaly layer, relying solely on whatever protections the underlying `tar` binary happens to provide, which is not a defense Gitaly controls or guarantees.

### Recommendation
Replace the raw `tar -xvf` shell invocation in `untar` with the same hardened, in-process tar-extraction logic used by `extractTarToDirectory` (or share that implementation), explicitly rejecting any entry whose resolved path, symlink target, or hard-link target escapes the destination directory, and rejecting absolute paths and unsupported types up front, consistent with the safeguards already implemented for `replicate.go`.

### Proof of Concept
1. Stand up an HTTP server serving a tar archive crafted with a member such as `../../../other-repo/.git/hooks/post-receive` (or a `TypeSymlink`/`TypeLink` entry pointing outside the target directory).
2. Call `CreateRepositoryFromSnapshot` with `HttpUrl` pointing at that server.
3. Observe that `s.untar` extracts the archive via `tar -C <targetRepoPath> -xvf -` with no path validation, in contrast to the equivalent hardened `extractTarToDirectory` path in `replicate.go`, potentially writing files outside the newly created repository's directory.

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

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L135-144)
```go
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

**File:** internal/gitaly/service/repository/replicate.go (L314-391)
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

```
