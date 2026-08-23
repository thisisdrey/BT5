### Title
Unsafe tar extraction from a caller-controlled HTTP URL enables arbitrary file write/overwrite - ([File: internal/gitaly/service/repository/create_repository_from_snapshot.go])

### Summary
The `CreateRepositoryFromSnapshot` RPC downloads an archive from a caller-supplied `HttpUrl` and pipes it directly into `tar -C <path> -xvf -` with no validation of the archive contents (no path-traversal checks, no symlink/hardlink restrictions, no member-type restrictions), unlike the equivalent snapshot-extraction code path in `replicate.go` which explicitly validates every tar entry.

### Finding Description
`untar()` fetches the response body of `in.GetHttpUrl()` and hands it straight to the external `tar` binary for extraction into the target repository path, with the code comment itself acknowledging the danger: [1](#0-0) [2](#0-1) 

This is the same bug class as the report's BabyJubjub finding — an operation is performed on untrusted, structured input (`BabyAdd`/`BabyDbl`/`BabyPbk` operate on curve points without calling `BabyCheck()`) without first validating that the input satisfies the invariants the operation assumes. Here, `untar()` performs filesystem writes based on attacker/caller-controlled tar member names without first validating that each entry resolves inside the target directory. In contrast, Gitaly's own `extractTarToDirectory()` (used by `replicate.go`'s snapshot-based replication) demonstrates the expected validation pattern — checking that regular files, symlinks, and hardlinks all resolve within `targetDir` before being written: [3](#0-2) [4](#0-3) 

The `CreateRepositoryFromSnapshot` handler validates only the target `Repository` identifier, not the archive content, before calling `untar`: [5](#0-4) 

### Impact Explanation
An archive containing tar entries with `../` path segments or absolute paths/symlinks could cause file writes outside of the intended repository directory on the Gitaly node, corrupting or overwriting arbitrary files reachable by the Gitaly process, or planting files (e.g., hook scripts) in unexpected locations. This maps to the "extraction escape" category called out as an acceptable finding class in this scan's validation rules.

### Likelihood Explanation
Exploitability depends on how much control an ordinary/unprivileged caller has over the `HttpUrl` (and, by extension, the archive contents) passed to `CreateRepositoryFromSnapshotRequest`, and whether the RPC is reachable outside of internal Gitaly-to-Gitaly/Praefect replication flows. The code's own comment ("The received archive is trusted a lot... it should undergo a lot of hardening") indicates the Gitaly maintainers are already aware this code path assumes a trusted archive source and is not intended to be exposed to untrusted endpoints. I could not fully confirm within the available tool calls whether the RPC is exclusively invoked over trusted internal replication channels (e.g., only from Praefect during replication) or could be triggered with an attacker-influenced URL from a less trusted context — this remains unverified due to the ask-only, read-limited nature of this investigation.

### Recommendation
Route `CreateRepositoryFromSnapshot`'s extraction through the same safe path used in `replicate.go`'s `extractTarToDirectory`/`extractFile`, which validates that every tar member (regular file, directory, symlink, and hardlink) resolves within the target directory before writing, instead of shelling out to the unconstrained `tar` binary.

### Proof of Concept
Not independently verified in this session. Conceptually: a caller providing an `HttpUrl` pointing to a server-controlled tar archive containing an entry named `../../../etc/cron.d/evil` (or a symlink entry pointing outside the extraction directory followed by a regular-file entry through that symlink) would, if reachable, cause `tar -C <path> -xvf -` to write outside the intended repository directory, since no validation equivalent to `extractTarToDirectory`'s boundary checks is performed before extraction.

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

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L123-127)
```go
func (s *server) CreateRepositoryFromSnapshot(ctx context.Context, in *gitalypb.CreateRepositoryFromSnapshotRequest) (*gitalypb.CreateRepositoryFromSnapshotResponse, error) {
	repository := in.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repository, storage.WithSkipRepositoryExistenceCheck()); err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
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

**File:** internal/gitaly/service/repository/replicate.go (L334-339)
```go
		targetPath := filepath.Join(targetDir, header.Name)

		if !strings.HasPrefix(targetPath, targetDir+string(os.PathSeparator)) &&
			targetPath != targetDir {
			return fmt.Errorf("invalid file path in tar: %s", header.Name)
		}
```

**File:** internal/gitaly/service/repository/replicate.go (L352-364)
```go
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
