### Title
CreateRepositoryFromSnapshot pipes an unauthenticated/attacker-influenced HTTP body directly into `tar -xvf -` with no path sanitization, allowing tar-slip extraction outside the repository - (File: internal/gitaly/service/repository/create_repository_from_snapshot.go)

### Summary
`Create` in `internal/gitaly/repoutil/create.go` seeds the newly created repository directory by invoking a caller-supplied `seedRepository` callback before atomically moving the repo into place. For `CreateRepositoryFromSnapshot`, that callback is `server.untar`, which fetches an HTTP response body and pipes it straight into the external `tar -C path -xvf -` command with no inspection of the archive's entry names. There is no code in `Create`, `untar`, or the `tar` invocation that verifies entries stay within `path`, so a malicious/compromised snapshot source can write files anywhere the Gitaly process user can reach on disk.

### Finding Description
`repoutil.Create` (`internal/gitaly/repoutil/create.go:79-148`) creates a temp repo directory and calls `seedRepository(newRepoProto)` to populate it, then does an atomic rename into the target path. It performs no validation whatsoever of what the seeding step writes to disk — that responsibility is delegated entirely to the caller.

For `CreateRepositoryFromSnapshot` (`internal/gitaly/service/repository/create_repository_from_snapshot.go:123-149`), the seed callback calls `s.untar(ctx, path, in)`, which:
1. Issues an HTTP GET to `in.GetHttpUrl()` (attacker-controlled field on the request), optionally through a resolved-address client to prevent DNS rebinding, and optionally sets an `Authorization` header from `in.GetHttpAuth()` (lines 87-113).
2. Pipes the raw response body into `tar -C path -xvf -` (lines 115-120) with **no archive validation** — no check for `../` relative entries, absolute paths, or symlink members before extraction.

The code even contains an explicit acknowledgment of this: *"NOTE: The received archive is trusted a lot. Before pointing this RPC at endpoints not under our control, it should undergo a lot of hardening."* (lines 140-141). This confirms the extraction path performs no containment check, meaning the invariant "extracted paths stay inside the target repository" is not enforced in Gitaly code at all — it depends entirely on the external `tar` binary's own protections against `..`/absolute-path members, which are not guaranteed across all `tar` implementations/flags and can be bypassed with symlink-based tricks (write a symlink entry pointing outside `path`, then write through it) since `-xvf` alone does not add `--no-overwrite-dir`/safe-extraction restrictions.

The unprivileged-attacker path is: the client that calls `CreateRepositoryFromSnapshot` fully controls `HttpUrl`, `HttpAuth`, and `ResolvedAddress` fields of the request, and the server at that URL fully controls the tar stream content — there is no server-side allowlist restricting the URL to trusted internal endpoints inside `untar` itself, and no sanitization of the tar member paths before or during extraction. ` [1](#0-0) `

### Impact Explanation
If reachable with an attacker-controlled `HttpUrl` (i.e., an attacking HTTP server that returns a crafted tar), the attacker can:
- Plant files outside the intended repository directory (tar/symlink extraction escape) via `../` or symlinked entries, since `Create`/`untar` never verify extracted paths remain under `path`.
- Cause GitLab-configured `HttpAuth` header values to be sent to whatever `HttpUrl` is supplied, which is a credential/auth-header disclosure risk if that URL is attacker-influenced.
- If `ResolvedAddress`/`HttpUrl` can be steered to an internal-only endpoint, this is also an SSRF vector, though `newResolvedHTTPClient` does perform some DNS-rebinding mitigation and disables redirects (`internal/gitaly/service/repository/create_repository_from_snapshot.go:41-46,50-85`), reducing (but not eliminating) SSRF risk relative to the extraction-escape issue.

This matches the "archive extraction escape planting files outside the repository" and potentially "credential/auth-header disclosure to an attacker host" GitLab bounty impact classes named in the question.

### Likelihood Explanation
The severity of this finding depends heavily on who can actually invoke `CreateRepositoryFromSnapshot` and set `HttpUrl`/`HttpAuth`/`ResolvedAddress`. This RPC is part of Gitaly's `RepositoryService`, which is generally invoked by GitLab Rails (e.g., for cross-shard repository moves/forks/snapshots), not directly by an end-user gRPC client. I could not determine from the available code whether GitLab Rails passes a user-influenceable URL into this field, or whether it always constructs the URL itself pointing at a trusted, internally-known Gitaly snapshot endpoint. The question's threat-model assumption ("attacker controls remote URL... entrypoint CreateRepositoryFromSnapshot") is asserted rather than demonstrated in this repo — Gitaly itself does not authenticate/restrict the `HttpUrl` to a known-good host inside `untar`, so if any component upstream (Rails) forwards attacker input into `HttpUrl` without validation, this code path is exploitable as described. Because Gitaly's own code performs no allowlisting or path-confinement, this is a real gap in defense-in-depth even if Rails is expected to be the only caller, matching the explicit in-code warning that the RPC is unsafe against untrusted endpoints.

### Recommendation
- Replace the external `tar -xvf -` invocation with Go's `archive/tar` reader, validating each header's `Name`/`Linkname` with `filepath.Clean` + a prefix check (or `storage.ValidateRelativePath`-style logic) to reject `../`, absolute paths, and symlink/hardlink entries whose target escapes `path`, before writing any file.
- Restrict `HttpUrl` (and equivalently the bundle-URI/remote-URL code paths) to an explicit allowlist of trusted internal Gitaly/Rails endpoints, or require it to be supplied out-of-band (e.g., resolved storage-internal address) rather than accepted verbatim from the RPC request.
- Avoid forwarding `HttpAuth` to hosts that aren't verified as trusted destinations.

### Proof of Concept
```go
// internal/gitaly/service/repository/create_repository_from_snapshot_poc_test.go
func TestCreateRepositoryFromSnapshot_TarSlip(t *testing.T) {
    // Start an HTTP server that serves a malicious tar containing:
    //   ../../../../tmp/pwned  (relative traversal)
    // or a symlink entry "link -> /tmp" followed by "link/pwned".
    srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        tw := tar.NewWriter(w)
        _ = tw.WriteHeader(&tar.Header{
            Name: "../../../../tmp/gitaly-poc-escape",
            Mode: 0o644,
            Size: int64(len("pwned")),
        })
        _, _ = tw.Write([]byte("pwned"))
        _ = tw.Close()
    }))
    defer srv.Close()

    cfg, client := setupRepositoryService(t)
    req := &gitalypb.CreateRepositoryFromSnapshotRequest{
        Repository: &gitalypb.Repository{StorageName: cfg.Storages[0].Name, RelativePath: "poc.git"},
        HttpUrl:    srv.URL,
    }
    _, err := client.CreateRepositoryFromSnapshot(ctx, req)
    require.NoError(t, err)

    // Expected (buggy) result: file exists outside the storage/repo dir.
    _, statErr := os.Stat("/tmp/gitaly-poc-escape")
    require.NoError(t, statErr, "tar entry escaped the target repository directory")
}
```
Expected assertion if vulnerable: `/tmp/gitaly-poc-escape` is created outside of the repository/storage root, demonstrating the extraction-escape described. ` [2](#0-1) `

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
