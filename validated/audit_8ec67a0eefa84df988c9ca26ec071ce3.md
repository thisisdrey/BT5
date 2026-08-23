### Title
Unbounded/unvalidated tar extraction in `CreateRepositoryFromSnapshot` via system `tar` on attacker-influenced HTTP response - ([File: internal/gitaly/service/repository/create_repository_from_snapshot.go])

### Summary
`CreateRepositoryFromSnapshotRequest` accepts `http_url`, `http_auth`, and `resolved_address` fields that are never bounds-/content-validated before Gitaly fetches the referenced content and pipes it directly into `tar -C path -xvf -` [1](#0-0) . This mirrors the report's bug class: security‑relevant parameters (`http_url`/`resolved_address`/`http_auth`, analogous to the Solidity report's unvalidated `vendor`/`royaltyFraction` constructor inputs) are accepted with only superficial checks and then used to drive privileged, hard-to-reverse operations (extracting an entire archive onto disk) with no limits on size, content, or extraction method validation beyond what the external `tar` binary itself enforces.

### Finding Description
`CreateRepositoryFromSnapshot` is a gRPC MUTATOR RPC. Its handler validates only the `Repository` field [2](#0-1) . The `HttpUrl`, `HttpAuth`, and `ResolvedAddress` fields, which control what remote content is fetched and how, undergo essentially no meaningful validation: `HttpUrl` is only parsed for URL well-formedness by `http.NewRequestWithContext` / `url.ParseRequestURI`, and `ResolvedAddress` is checked only for being a syntactically valid IP [3](#0-2) . There is no restriction on scheme beyond a switch on port defaulting, no allow-list of destinations, and no limit on response size before piping it into `tar`.

The code's own comment acknowledges the danger: *"NOTE: The received archive is trusted a lot. Before pointing this RPC at endpoints not under our control, it should undergo a lot of hardening."* [4](#0-3) . The HTTP response body is streamed unbounded directly as stdin to the external `tar` binary with no size cap, no timeout on the extraction, and no post-extraction validation of what was written (unlike the hardened, path-checked `extractTarToDirectory` Go-native implementation used elsewhere in the same package for `Replicate`'s `GetSnapshot`-based repo creation, which explicitly validates symlink/hardlink targets and absolute-path traversal [5](#0-4) ). This RPC uses the far less hardened `tar` shell-out path instead.

Because the destination directory passed to `tar -C path` is the newly created (empty) repository path, whether GNU tar can be coerced to escape that directory depends entirely on the content of the untrusted archive (e.g., via absolute paths or `--` disabled protections in older/BSD `tar` implementations) and receives none of the additional Go-side safety checks that the sibling `extractTarToDirectory` function performs.

### Impact Explanation
An attacker who can influence the parameters of `CreateRepositoryFromSnapshot` (this RPC is invoked internally by Praefect/Gitaly during repository replication and snapshot-based repository creation; the `http_url`/`resolved_address`/`http_auth` fields are attacker-controllable inputs to this specific RPC surface) can:
- Cause resource exhaustion on the Gitaly node by returning an arbitrarily large HTTP response with no size limit before it is piped to `tar`.
- Potentially escape the intended extraction directory depending on the `tar` binary/version behavior with maliciously crafted archive entries (absolute paths, `..` traversal, symlinks), since none of these are checked in Go before invocation, unlike the parallel `extractTarToDirectory` code path that explicitly guards against exactly these cases.
- Perform SSRF-adjacent requests to arbitrary internal endpoints since `HttpUrl` is not restricted to expected/allow-listed hosts, only that it parses as a URL.

This is a genuine "insufficient input validation" issue directly analogous to the report: attacker/caller-supplied configuration values (`http_url`, `resolved_address`, `http_auth`) are trusted for use in a powerful, hard-to-undo operation (archive extraction to disk) without bounds or content checks.

### Likelihood Explanation
The likelihood depends on which callers can reach `CreateRepositoryFromSnapshot` with attacker-influenced `HttpUrl`/`ResolvedAddress` in a given deployment; I could not fully confirm from the indexed code the exact set of authorized callers/production wiring for this specific RPC (e.g., whether it's gated to Praefect-only internal replication traffic). This should be treated as uncertain and verified with a full checkout, since the code's own comment already flags it as insufficiently hardened for use with untrusted endpoints.

### Recommendation
1. Enforce a maximum response size (e.g., via `io.LimitReader` around `rsp.Body`) before piping into `tar`.
2. Replace the shell-out to `tar` with the same Go-native, path-validating extraction logic already used in `extractTarToDirectory` (which checks for absolute symlinks, traversal via `..`, and validates each `targetPath` stays within `targetDir`) [6](#0-5) .
3. Restrict `HttpUrl` scheme/host to an explicit allow-list or require it match the resolved Gitaly-internal snapshot endpoint, rather than accepting arbitrary URLs.
4. Add an extraction timeout and abort large/slow transfers.

### Proof of Concept
Not independently reproducible from the indexed code alone — the request payload for `CreateRepositoryFromSnapshotRequest.http_url`, `resolved_address`, and `http_auth` would need to be issued against a Gitaly instance with a malicious HTTP server returning either (a) a multi-terabyte stream to trigger resource exhaustion, or (b) a tar archive with absolute-path/symlink entries to test extraction-directory escape, and observe whether the untarred content lands outside the intended repository path. This proof-of-concept construction and verification against a live/checked-out `tar` binary's actual behavior could not be completed within the scope of this read-only, index-based review.

### Citations

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L50-71)
```go
func newResolvedHTTPClient(httpAddress, resolvedAddress string) (*http.Client, error) {
	url, err := url.ParseRequestURI(httpAddress)
	if err != nil {
		return nil, structerr.NewInvalidArgument("parsing HTTP URL: %w", err)
	}

	port := url.Port()
	if port == "" {
		switch url.Scheme {
		case "http":
			port = "80"
		case "https":
			port = "443"
		default:
			return nil, structerr.NewInvalidArgument("unsupported schema %q", url.Scheme)
		}
	}

	// Sanity-check whether the resolved address is a valid IP address.
	if net.ParseIP(resolvedAddress) == nil {
		return nil, structerr.NewInvalidArgument("invalid resolved address %q", resolvedAddress)
	}
```

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L87-120)
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
```

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L123-127)
```go
func (s *server) CreateRepositoryFromSnapshot(ctx context.Context, in *gitalypb.CreateRepositoryFromSnapshotRequest) (*gitalypb.CreateRepositoryFromSnapshotResponse, error) {
	repository := in.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repository, storage.WithSkipRepositoryExistenceCheck()); err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}
```

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L139-141)
```go
		//
		// NOTE: The received archive is trusted *a lot*. Before pointing this RPC
		// at endpoints not under our control, it should undergo a lot of hardening.
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
