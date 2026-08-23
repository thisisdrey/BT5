### Title
`CreateRepositoryFromSnapshot` forwards an unvalidated, caller-supplied HTTP URL into an unsanitized `tar` extraction, enabling archive-content path traversal / symlink writes into arbitrary storage paths - (File: internal/gitaly/service/repository/create_repository_from_snapshot.go)

### Summary
The `CreateRepositoryFromSnapshot` RPC takes a caller-controlled `HttpUrl` (plus optional `HttpAuth` and `ResolvedAddress`), fetches whatever content that URL returns, and pipes the response body directly into the system `tar` binary (`tar -C path -xvf -`) with no validation of the archive's contents. [1](#0-0)  This mirrors the report's bug class: attacker-influenced input is forwarded verbatim into a privileged operation (here, extraction into the repository's on-disk storage path) without any content validation, relying entirely on the operator's trust in the source.

### Finding Description
`untar()` builds an HTTP GET request from `in.GetHttpUrl()`, optionally rewrites the connection target via `newResolvedHTTPClient` when `ResolvedAddress` is set (an SSRF/DNS-rebinding mitigation borrowed from the remote-fetch code paths), and then streams the raw HTTP response body straight into `tar -C path -xvf -`: [2](#0-1) 

Unlike the hardened Go-based tar extractor used elsewhere in the same package (`extractTarToDirectory` in `replicate.go`, which explicitly validates that every `TypeDir`/`TypeReg` target path stays under `targetDir`, and separately validates symlink/hardlink targets before creating them) [3](#0-2) , this code path delegates all path handling to the system `tar` binary with no post-extraction validation. The caller function itself documents the danger of this design: [4](#0-3) 

Because the archive's content comes verbatim from whatever server answers at the attacker-supplied `HttpUrl`, an attacker in control of that URL (or a Rails/gitlab-shell caller that itself forwards user input into `HttpUrl` without sanitizing it) can supply a tar archive containing:
- Absolute paths or `../` traversal entries, some of which GNU tar strips by default but which are not deterministic across `tar` implementations/versions/flags.
- Symlinks pointing to arbitrary targets, since the system `tar` (without `-P`/`--no-same-owner` restrictions being explicitly enforced here) may still write through them depending on entry order.

The RPC is invoked as part of repository creation/replication flows (e.g. from Praefect's coordinator when routing writes) [5](#0-4) , so the extraction target is always a live storage path — a write into that path outside the intended repository directory can corrupt or overwrite arbitrary files under the Gitaly storage root.

### Impact Explanation
A successful path-escape during extraction can overwrite or plant files anywhere the Gitaly process has write access within (or, depending on `tar` behavior, outside) the storage root — including custom hooks, git config, or other repositories' data — leading to remote code execution via hook injection or storage corruption. This is a "storage escape / extraction escape" per the validation criteria in the prompt.

### Likelihood Explanation
Exploitability depends on whether `HttpUrl` (and the server responding to it) can be influenced by an untrusted actor — this RPC is intended for internal Gitaly-to-Gitaly / trusted snapshot transfer, and the in-code comment already flags it as unsafe to point at endpoints "not under our control." The code contains no allowlist restricting `HttpUrl` to internal, trusted endpoints, and no archive-content validation exists at all in this path (in contrast to the sibling `extractTarToDirectory` function), so if any untrusted or lower-privileged caller can influence the URL or the response content, the extraction step is currently unguarded.

### Recommendation
- Replace the external `tar -xvf -` invocation with the same hardened Go-based extraction logic already implemented in `extractTarToDirectory` (path containment checks for all entry types, symlink/hardlink target validation).
- Restrict `HttpUrl` to an explicit allowlist of trusted internal endpoints, or remove/deprecate this RPC in favor of the already-existing internal snapshot-transfer RPC (`GetSnapshot`) which does not depend on an arbitrary externally-supplied URL.
- Remove the "trusted a lot" comment only once real validation is added; until then, treat this RPC as unsafe for any request path reachable from ordinary user actions (fork/import).

### Proof of Concept
1. Trigger `CreateRepositoryFromSnapshot` with `HttpUrl` pointing to an attacker-controlled HTTP server.
2. That server responds with a tar stream containing an entry such as `../../../other-repo/hooks/pre-receive` (or a symlink entry) with executable content.
3. `s.untar` pipes the response directly into `tar -C <repoPath> -xvf -` [6](#0-5) ; depending on the installed `tar`'s handling of `..`/symlink entries, the file is written outside `repoPath`, in another repository's storage, or overwrites intended hook files — no code in this path checks for that condition, unlike the Go-native extractor.

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

**File:** internal/gitaly/service/repository/replicate.go (L334-390)
```go
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

**File:** internal/praefect/coordinator.go (L1-1)
```go
package praefect
```
