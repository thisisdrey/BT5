### Title
Unsanitized tar extraction in `untar` allows path traversal/symlink escape outside repository directory - (File: internal/gitaly/service/repository/create_repository_from_snapshot.go)

### Summary
`CreateRepositoryFromSnapshot` streams an attacker-controlled HTTP response body directly into `tar -C path -xvf -` with no `-P`/absolute-path rejection, no `--no-same-owner`, and no post-extraction validation of extracted entry names or symlink targets. Since `in.GetHttpUrl()` is fully attacker-controllable, a malicious tar stream can contain `../` traversal entries or symlink entries that cause writes outside the newly created repository directory.

### Finding Description
`untar` builds the extraction command as a raw string list `[]string{"tar", "-C", path, "-xvf", "-"}` and pipes the HTTP response body from `in.GetHttpUrl()` as stdin via `command.WithStdin(rsp.Body)` [1](#0-0) . The URL, resolved address, and auth header are all attacker-supplied request fields with no restriction on the target host once the RPC is reachable [2](#0-1) . There is no wrapping of GNU tar with `-P`-disabling defaults are actually already off by default for GNU tar (absolute paths are stripped by default), but `../` relative traversal entries and symlink members are not blocked by system `tar` and will be followed/created relative to `-C path`, allowing writes outside `path` when the archive contains crafted parent-relative names or symlinks pointing outside the target directory. The code comment directly acknowledges this is unsafe: "NOTE: The received archive is trusted *a lot*. Before pointing this RPC at endpoints not under our control, it should undergo a lot of hardening." [3](#0-2) . No call to `storage.ValidateRelativePath` or any archive member validation occurs between receiving the stream and invoking `tar`.

### Impact Explanation
A successful traversal/symlink entry in the tar stream lets an attacker overwrite files in other repositories on the same storage (e.g. `../other-repo/hooks/pre-receive`) or elsewhere on disk reachable by the Gitaly process user, resulting in cross-repository hook injection / command execution or storage confinement bypass — matching GitLab's "arbitrary file write" / "path traversal leading to RCE" bounty impact class.

### Likelihood Explanation
Exploitability fully depends on whether `CreateRepositoryFromSnapshot` and its `HttpUrl`/`ResolvedAddress` fields are reachable by an unprivileged, non-admin actor through GitLab Rails (e.g., project import/fork/mirroring flows that proxy to this RPC). The code and proto files in this repo do not show any additional authorization/URL-allowlisting layer inside Gitaly itself for this RPC beyond what GitLab Rails may impose, and the in-code comment confirms the archive is "trusted a lot" and unhardened for use with untrusted endpoints, indicating this exposure was a known, accepted risk contingent on the caller only pointing this at trusted endpoints.

### Recommendation
Do not shell out to system `tar` on an unsanitized attacker stream. Extract using Go's `archive/tar` package with explicit validation per entry: reject absolute paths, reject entries whose cleaned path escapes `path` (via `filepath.Clean`/`filepath.Rel` check), reject or safely resolve symlink entries (ensuring targets stay within `path`), and drop unsupported entry types (device nodes, hardlinks to outside paths). Alternatively, restrict `HttpUrl`/`ResolvedAddress` to a caller-provided allowlist enforced at the Gitaly layer if the RPC's use case only requires trusted internal endpoints.

### Proof of Concept
```go
func TestUntarPathTraversal(t *testing.T) {
    ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        tw := tar.NewWriter(w)
        // crafted entry escaping the repo directory
        content := []byte("#!/bin/sh\necho pwned\n")
        hdr := &tar.Header{
            Name: "../../other-repo/hooks/pre-receive",
            Mode: 0755,
            Size: int64(len(content)),
        }
        tw.WriteHeader(hdr)
        tw.Write(content)
        tw.Close()
    }))
    defer ts.Close()

    // call CreateRepositoryFromSnapshot with HttpUrl: ts.URL
    // assert that "other-repo/hooks/pre-receive" was created/modified
    // outside the new repository's own relative path directory.
}
```

### Citations

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L87-103)
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
```

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L105-120)
```go
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

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L140-144)
```go
		// NOTE: The received archive is trusted *a lot*. Before pointing this RPC
		// at endpoints not under our control, it should undergo a lot of hardening.
		if err := s.untar(ctx, path, in); err != nil {
			return structerr.NewInternal("extracting snapshot: %w", err)
		}
```
