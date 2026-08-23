This is a genuinely interesting finding, explicitly acknowledged in the code's own comment as untrusted/unhardened. Let me verify the extraction is indeed unguarded compared to sibling code that has explicit tar-slip protections.### Title
Unvalidated Tar Extraction in `CreateRepositoryFromSnapshot` Allows Path Traversal / Arbitrary File Write - (File: internal/gitaly/service/repository/create_repository_from_snapshot.go)

### Summary
The `CreateRepositoryFromSnapshot` RPC downloads an archive from a caller-supplied HTTP URL and pipes its raw bytes straight into a spawned `tar -C path -xvf -` process with no per-entry validation of file paths, symlink targets, or absolute paths. This is the same bug class as the "no constraints beyond authorization" report, applied to Gitaly's unprivileged storage/extraction surface: any authenticated Gitaly client that can invoke this ordinary repository-creation RPC controls the archive content and can attempt to escape the target directory during extraction, unlike sibling extraction code paths in the same codebase which explicitly validate entries.

### Finding Description
`untar()` fetches an HTTP response body (`in.GetHttpUrl()`, optionally with `in.GetHttpAuth()` and `in.GetResolvedAddress()` for DNS-rebinding mitigation) and hands it directly to the external `tar` binary: [1](#0-0) 

The extraction happens with no path/symlink checks performed by Gitaly itself — it relies entirely on whatever protections the system `tar` binary happens to implement. The code's own comment acknowledges this is unhardened: [2](#0-1) 

This is inconsistent with how Gitaly handles tar extraction elsewhere in the same codebase, where explicit validation against directory traversal, absolute paths, and symlink escapes is implemented: [3](#0-2) 

The `CreateRepositoryFromSnapshot` RPC is a normal, unprivileged, ordinary-user-reachable gRPC endpoint (part of `RepositoryService`), taking a fully attacker/caller-controlled `HttpUrl`, `HttpAuth`, and archive payload as protobuf request fields — this matches the "archive or bundle extraction escape" and "crafted RPC field" categories called out in the validation rules.

### Impact Explanation
If the target HTTP endpoint (attacker-controlled, or a compromised/malicious upstream serving the snapshot) returns a tar archive containing entries with `../` traversal sequences or absolute paths, extraction could write files outside the intended repository directory on the Gitaly storage node, subject only to whatever protections the invoked `tar(1)` binary provides (which vary by implementation/version and are not verified or pinned by Gitaly). This is a storage-escape / arbitrary file write primitive on the Gitaly node, potentially affecting other repositories or the node's filesystem.

### Likelihood Explanation
The RPC is reachable by any client authorized to call ordinary Gitaly repository RPCs (no special privilege check beyond normal repository target validation) and the archive content is entirely determined by the response from the caller-specified `HttpUrl`. The comment in the source explicitly documents that this code path is only currently safe because it's "pointed at endpoints under our control" — i.e., the security relies on caller trust rather than Gitaly's own validation, which is a fragile assumption for a general-purpose gRPC service.

### Recommendation
Replace the direct pipe into external `tar(1)` with Gitaly's own validated extraction routine (as already implemented in `extractTarToDirectory` in `internal/gitaly/service/repository/replicate.go`), which rejects absolute paths, validates that resolved paths (including symlink targets) stay within the target directory, and handles each tar entry type explicitly rather than delegating to the OS `tar` binary's byte stream trust.

### Proof of Concept
1. Stand up an HTTP server that returns a tar archive containing an entry named e.g. `../../../../tmp/pwned` (or an absolute path `/tmp/pwned`, or a symlink entry pointing outside the extraction root followed by a regular-file entry through that symlink).
2. Call `CreateRepositoryFromSnapshot` with `HttpUrl` pointing at that server.
3. Observe whether `internal/gitaly/service/repository/create_repository_from_snapshot.go`'s `untar()` writes the file outside the newly created repository path — the exact outcome depends on the `tar` binary's own path-sanitization behavior, since Gitaly performs no independent validation, unlike its own `extractTarToDirectory` implementation which is proven (by `TestExtractTarToDirectory_SymlinkValidation`) to reject such entries.

**Note on confidence**: I was not able to fully determine, from static review alone, which exact `tar` implementation/version/flags run in production Gitaly deployments and whether that specific `tar` binary's built-in protections fully neutralize every traversal/symlink vector (this depends on the OS/container image and `tar` version, which the index does not let me confirm). If precise reproduction with the actual production `tar` binary is required, a Devin session with filesystem/terminal access would be needed to build and run the PoC end-to-end.

### Citations

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

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L134-144)
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
