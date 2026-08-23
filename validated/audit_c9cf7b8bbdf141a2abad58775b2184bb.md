## Title
Tar extraction escape via `CreateRepositoryFromSnapshot`'s unsanitized system `tar` invocation - (File: `internal/gitaly/service/repository/create_repository_from_snapshot.go`)

### Summary
`CreateRepositoryFromSnapshot` fetches an attacker/caller-influenced archive from a request-supplied HTTP URL and pipes it directly into the system `tar` binary for extraction, without any Gitaly-side validation of archive entry names, symlink targets, or hard-link targets. This is the same class of bug as the "unsanitized merge into a structure with no key/path validation" issue described in the reference report (attacker-supplied structure content is trusted and merged/extracted without boundary checks), here manifesting as a potential path-traversal / extraction-escape in Gitaly's own tar handling rather than JS prototype pollution.

### Finding Description
The `untar` helper builds an HTTP request to `in.GetHttpUrl()` (a field fully controlled by the RPC caller) and streams the response body straight into `tar -C path -xvf -`: [1](#0-0) 

Notably, the code itself documents the risk: [2](#0-1) 

Unlike this code path, Gitaly's other tar-extraction implementation (`extractTarToDirectory`, used for repository snapshot replication) explicitly parses each tar header and rejects absolute symlinks, hard-link targets, and any resolved path that escapes the target directory: [3](#0-2) 

`CreateRepositoryFromSnapshot`'s `untar`, by contrast, delegates entirely to the system `tar` binary with no equivalent Gitaly-side header inspection — there is no check on `header.Name` for `..` traversal, no check on symlink/hardlink targets, and no restriction preventing the archive from writing files (via symlinks or relative traversal) outside `path`. This mirrors the "no key/path validation before merge/write" root cause in the reference report: the archive is "trusted a lot" and merged onto disk verbatim.

### Impact Explanation
If the `http_url` (and optionally `resolved_address`/`http_auth`) supplied in the `CreateRepositoryFromSnapshotRequest` points at a server the caller controls or can influence (e.g., a compromised/attacker-controlled mirror during a project import/migration flow), a maliciously crafted tar stream can:
- Traverse outside the newly created repository directory via `../` segments or symlink members, and overwrite/create files elsewhere on the storage or host filesystem (extraction escape / storage escape), subject to the OS `tar` binary's own protections (which are not guaranteed and vary across GNU/BSD tar versions and invocation flags).
- Potentially disclose credentials if `http_auth` is echoed to a malicious/attacker-influenced endpoint via redirects or DNS tricks (partially mitigated by disabling redirect-following, but `ResolvedAddress`/DNS behavior is caller-influenced).

### Likelihood Explanation
Reaching this path requires the RPC to be invoked with attacker-influenced `http_url`. This is reachable through Gitaly's gRPC surface (not requiring a privileged actor, malicious peer, or leaked token) whenever a caller can direct Gitaly's fetch target — e.g., an import/migration pipeline that lets an ordinary user supply an external source location. The comment in the code acknowledging the archive is "trusted *a lot*" and needs hardening "before pointing this RPC at endpoints not under our control" suggests this exposure is a known, accepted-but-unresolved risk rather than a defended path, unlike the hardened `extractTarToDirectory` used elsewhere.

### Recommendation
Replace the shelled-out `tar -xvf -` invocation with the same Go-native, validating extraction logic already used in `extractTarToDirectory` (reject absolute symlinks/hardlinks, verify every resolved path stays within `path`), or otherwise pre-validate every tar header before letting the system `tar` binary write to disk. Additionally, ensure `http_url`/`resolved_address` inputs are limited to trusted internal endpoints wherever this RPC is exposed to caller-influenced values.

### Proof of Concept
1. Invoke `CreateRepositoryFromSnapshot` with `http_url` pointing at an attacker-controlled HTTP server.
2. The server returns a tar stream containing an entry such as a symlink `payload -> ../../../etc/cron.d/evil` followed by a regular file entry named `payload` with attacker content, or a regular file entry named `../../outside.git/hooks/pre-receive`.
3. Because `untar` (`internal/gitaly/service/repository/create_repository_from_snapshot.go:87-121`) performs no header/path validation itself, the outcome depends solely on the system `tar` binary's protections; in configurations/tar versions where such protections are weak or bypassable (e.g., via symlink pre-staging, a known general tar-extraction class of issue), files land outside the intended repository directory `path`.

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

**File:** internal/gitaly/service/repository/replicate.go (L314-365)
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

```
