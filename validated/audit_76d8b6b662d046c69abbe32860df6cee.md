### Title
Path traversal via unsanitized tar extraction in `CreateRepositoryFromSnapshot` - (File: `internal/gitaly/service/repository/create_repository_from_snapshot.go`)

### Summary
`CreateRepositoryFromSnapshot` fetches an archive from a caller-supplied `HttpUrl` and pipes the raw HTTP response body directly into a shelled-out `tar -C path -xvf -` invocation with no validation of the tar entry names, symlink targets, or hardlink targets. This is analogous to the Rubicon `tailOff`/`rebalance` bug class: a caller-controlled parameter (there, `_stratUtil`/`filledAssetToRebalance`; here, `HttpUrl`/the archive body it serves) is trusted without the checks that an equivalent, security-conscious code path in the same codebase already performs.

### Finding Description
The `untar` helper does:
```go
cmd, err := command.New(ctx, s.logger, []string{"tar", "-C", path, "-xvf", "-"}, command.WithStdin(rsp.Body))
``` [1](#0-0) 

There is no inspection of the archive's entries before or during extraction — no rejection of `../` path components, no rejection of absolute paths, and no validation of symlink/hardlink targets. The code even documents the danger:
```go
// NOTE: The received archive is trusted *a lot*. Before pointing this RPC
// at endpoints not under our control, it should undergo a lot of hardening.
if err := s.untar(ctx, path, in); err != nil {
``` [2](#0-1) 

Contrast this with the sibling snapshot-extraction implementation used for `ReplicateRepository`, which extracts tar archives with Go's `archive/tar` package and explicitly validates every path, absolute symlinks, relative symlink escapes, and hardlink targets against the destination boundary:
```go
targetPath := filepath.Join(targetDir, header.Name)
if !strings.HasPrefix(targetPath, targetDir+string(os.PathSeparator)) &&
    targetPath != targetDir {
    return fmt.Errorf("invalid file path in tar: %s", header.Name)
}
...
case tar.TypeSymlink:
    if filepath.IsAbs(header.Linkname) {
        return fmt.Errorf("absolute symlink not allowed: %s -> %s", header.Name, header.Linkname)
    }
    resolvedTarget := filepath.Join(filepath.Dir(targetPath), header.Linkname)
    if !strings.HasPrefix(resolvedTarget, targetDir+string(os.PathSeparator)) &&
        resolvedTarget != targetDir {
        return fmt.Errorf("symlink target escapes extraction directory: %s -> %s", header.Name, header.Linkname)
    }
``` [3](#0-2) 

That hardened path even has a dedicated regression test suite (`TestExtractTarToDirectory_SymlinkValidation`) covering `../` and absolute symlink escapes. [4](#0-3) 

`CreateRepositoryFromSnapshot`'s `untar`, however, never received this hardening. The only protection applied to `HttpUrl`/`ResolvedAddress` is DNS-rebinding mitigation (pinning the resolved IP):
```go
func newResolvedHTTPClient(httpAddress, resolvedAddress string) (*http.Client, error) {
    url, err := url.ParseRequestURI(httpAddress)
    ...
``` [5](#0-4) 

That mitigation addresses SSRF only, not the trust placed in the *content* of the response, which is fed straight into `tar -x` unfiltered — mirroring the Rubicon issue where one check (address whitelisting) was implemented for `_stratUtil` conceptually similar to a URL allowlist, but the more important content/asset validation (`filledAssetToRebalance != underlyingToken`) was missing until patched.

### Impact Explanation
Because `path` passed to `tar -C path -xvf -` is the destination and the archive content is fully attacker/URL-controlled, a crafted tar stream containing entries like `../../../../etc/something` or a malicious symlink can write or overwrite files outside the intended repository directory on the Gitaly node's storage, i.e., a concrete storage escape from an extraction handler. Depending on file targets reachable by the Gitaly process user, this can corrupt or plant files in other repositories/storages or Gitaly's data directories.

### Likelihood Explanation
`CreateRepositoryFromSnapshot` is a standard RPC reachable by any authorized Gitaly/GitLab client performing a repository import/migration flow — it is registered in Praefect's coordinator and routed like other repository RPCs. [6](#0-5) 
The caller fully controls `HttpUrl`, `HttpAuth`, and `ResolvedAddress`, and by extension the content of the archive returned by that URL (e.g., by hosting content on a URL Gitaly is instructed to fetch from, which is exactly the "crafted RPC field" scenario the codebase's own comment ("before pointing this RPC at endpoints not under our control...") acknowledges as unresolved.

### Recommendation
Replace the shell `tar` invocation in `untar` with the same hardened Go-native extraction logic already implemented in `internal/gitaly/service/repository/replicate.go` (`extractTarToDirectory`/`extractFile`), which validates every entry's resolved path against the destination boundary and rejects absolute/`..`-escaping paths, absolute symlinks, and hardlink/symlink targets that escape the target directory.

### Proof of Concept
1. Set up an HTTP server that responds to a `GET` request with a tar stream containing an entry named `../../../../tmp/pwned` (a regular file) or a symlink entry `link -> ../../../../etc/cron.d/evil` followed by a regular file written through that symlink.
2. Call `CreateRepositoryFromSnapshot` with `HttpUrl` pointing at that server (optionally with `ResolvedAddress` set to bypass DNS-rebinding checks, since that check only pins the IP and does not inspect the response body).
3. Observe that `s.untar` (`internal/gitaly/service/repository/create_repository_from_snapshot.go:87-121`) pipes the raw HTTP body into `tar -C <repoPath> -xvf -` with no per-entry path validation, causing the malicious entry to be written outside `<repoPath>`, unlike the equivalent, guarded extraction path in `replicate.go`'s `extractTarToDirectory`.

### Citations

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L50-66)
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
```

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L115-120)
```go
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

**File:** internal/gitaly/service/repository/replicate.go (L334-364)
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
```

**File:** internal/gitaly/service/repository/replicate_test.go (L821-885)
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
```

**File:** internal/praefect/coordinator.go (L1-1)
```go
package praefect
```
