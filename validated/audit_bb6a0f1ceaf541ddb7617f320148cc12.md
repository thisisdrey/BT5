I found the strongest analog: `CreateRepositoryFromSnapshot`'s `untar` extracts an attacker-influenced tar archive using the raw `tar` binary with **no path/link validation whatsoever**, whereas the sibling code path (`ReplicateRepository`'s `extractTarToDirectory` in the same package) received an explicit fix that validates `TypeReg`/`TypeDir` targets and rejects `TypeSymlink`/`TypeLink` entries that escape the destination directory. This mirrors the report's pattern: one path got the containment fix, the analogous path did not.

### Title
Unrestricted tar extraction allows path/symlink escape in `CreateRepositoryFromSnapshot` - (File: internal/gitaly/service/repository/create_repository_from_snapshot.go)

### Summary
`CreateRepositoryFromSnapshot` extracts a remote HTTP tar archive directly with the system `tar` binary and no validation of entry names or link targets, while the sibling repository-replication code path (`extractTarToDirectory` in `replicate.go`) explicitly validates every tar entry's target path and rejects symlinks/hardlinks that escape the destination directory. The snapshot-creation path lacks this containment check entirely.

### Finding Description
`untar` builds and runs `tar -C path -xvf -` directly against the HTTP response body with no inspection of the archive contents beforehand: [1](#0-0) 
The handler's own comment acknowledges the archive is "trusted a lot" and needs hardening before being pointed at untrusted endpoints: [2](#0-1) 
Compare this to `extractTarToDirectory`, used by `ReplicateRepository`, which computes and validates every `targetPath` against `targetDir`, and additionally validates symlink and hardlink targets to ensure they resolve within the extraction boundary before creating them: [3](#0-2) 
That containment logic (and its regression tests) demonstrates the project is aware of and has fixed this exact bug class for one extraction code path, but the `CreateRepositoryFromSnapshot` path — which extracts data fetched from an arbitrary, request-supplied `HttpUrl` — was never given equivalent protection, relying entirely on GNU `tar`'s own (much weaker, no built-in symlink-target confinement) defaults.

### Impact Explanation
`CreateRepositoryFromSnapshot` is a `MUTATOR` RPC that takes an `HttpUrl` (and optional `HttpAuth`/`ResolvedAddress`) supplied in the request, fetches whatever tar stream that URL returns, and extracts it verbatim into the newly created repository's directory on disk. Because there is no post-extraction validation of paths, an archive containing:
- Entries with `../../` path traversal in their names, or
- Symlinks pointing outside the target directory, followed by regular-file entries written through that symlink,

can write or overwrite arbitrary files on the Gitaly node's filesystem, outside the intended repository storage path. This is a classic "tar slip" that leads to storage escape / arbitrary file write in the context of the Gitaly process.

### Likelihood Explanation
This is reachable by any caller who can invoke `CreateRepositoryFromSnapshot` and control (or influence, e.g., via a compromised/malicious import source) the `HttpUrl` field — a supported "import" style workflow (fetching repository content from an external URL) that is explicitly in scope. The code comment itself flags this as an unresolved hardening gap, increasing confidence that this is a genuine, currently-unmitigated weakness rather than a false positive.

### Recommendation
Replace the raw `tar -C path -xvf -` invocation with the same (or shared) validated extraction logic used in `extractTarToDirectory`: parse the tar stream with Go's `archive/tar`, and for every entry compute the joined target path and reject/normalize any entry (including `TypeSymlink`/`TypeLink`) whose resolved target does not stay within the destination directory, exactly as already implemented for `ReplicateRepository`. Ideally, factor the validated extraction routine out into a shared helper so both code paths use one hardened implementation.

### Proof of Concept
1. Host an HTTP server serving a tar archive containing an entry such as `../../../../tmp/evil` (path traversal) or a symlink entry `link -> ../../../../etc` followed by a regular file entry `link/passwd` with attacker content.
2. Call `CreateRepositoryFromSnapshot` with `HttpUrl` pointing at that server.
3. `untar` invokes `tar -C <repoPath> -xvf -` on the fetched stream with no path containment checks (`create_repository_from_snapshot.go:115-121`), unlike the hardened `extractTarToDirectory` path (`replicate.go:334-403`), allowing the tar entries to write outside `repoPath`.

### Citations

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L115-121)
```go
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

**File:** internal/gitaly/service/repository/replicate.go (L314-403)
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
```
