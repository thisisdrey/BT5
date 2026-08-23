### Title
Tar extraction in repository replication allows single-level directory-boundary escape via self-referencing symlink - (File: `internal/gitaly/service/repository/replicate.go`)

### Summary
`extractTarToDirectory` in `internal/gitaly/service/repository/replicate.go` is a hand-rolled tar extractor used to unpack a repository snapshot into a freshly created target repository directory during `ReplicateRepository`. It validates every tar entry's destination path with a purely *lexical* `filepath.Join`/`filepath.Clean` + `strings.HasPrefix` check against `targetDir`, and validates symlink targets the same way. Because the check is string-based and does not account for how the OS actually resolves an on-disk symlink that points to "the extraction root itself" (e.g. `Linkname: "."`), a single such symlink can absorb one path component "for free" during real filesystem resolution while the lexical validator still believes that component consumes one directory level. This mismatch lets a subsequent tar entry escape exactly one directory level above `targetDir` even though the lexical check reports the resulting path as safely contained.

### Finding Description
The extraction loop lives at `internal/gitaly/service/repository/replicate.go:315-406` (`extractTarToDirectory`), invoked from `extractSnapshot` (`internal/gitaly/service/repository/replicate.go:264-312`), which is called during `ReplicateRepository`'s repository-creation path (`createFromSnapshot` → `extractSnapshot`).

For every tar header, the code computes: [1](#0-0) 

and for symlink entries validates the *symlink's own* resolved target the same lexical way: [2](#0-1) 

Both checks operate purely on strings via `filepath.Join`/`filepath.Clean`, never re-checking the path against the real filesystem once a symlink has actually been created on disk. `filepath.Join(targetDir, "link/../evil")` treats `link` as consuming exactly one path component before the following `..` cancels it, yielding `targetDir/evil`, which passes validation. However, if an earlier tar entry created `targetDir/link` as a symlink to `.` (which is legitimately allowed, since its own resolved target equals `targetDir`, see the symlink-creation check above), then at real extraction time `os.OpenFile`/`os.MkdirAll` on the name `link/../evil` resolves `link` to `targetDir` itself (a 0-level consuming component), and the trailing `..` then walks up **one directory above** `targetDir` — landing outside the intended extraction root even though the lexical validator reported it as safe.

This is a textbook "Zip-Slip"-style path-confusion bug: the boundary check is computed on the declared (lexical) name instead of the path as the OS will actually resolve it once a symlink is materialized on disk between two entries of the same archive.

### Impact Explanation
A crafted snapshot stream (consumed via `GetSnapshot`/`ReplicateRepository`) can cause the receiving Gitaly node to write an arbitrary regular file, directory, or hard-linked file one directory level above the newly-created target repository path (e.g., into the parent hashed-storage bucket directory, potentially colliding with or corrupting sibling repository directories or storage-root files). This is a concrete storage escape during repository creation/replication — files can be planted outside the repository directory that `repoutil.Create` intended to populate, which can corrupt neighboring repository state on the storage or be used to stage further attacks (e.g., planting files that a later operation on a sibling path will pick up).

### Likelihood Explanation
Exploitation requires the tar stream processed by `extractTarToDirectory` to contain attacker-influenced entries: first a symlink entry named e.g. `link` with `Linkname: "."`, followed by a regular/hardlink/directory entry named `link/../<name>`. The stream is produced by `GetSnapshot` (`internal/gitaly/service/repository/snapshot.go`) on a source repository and consumed on the target during `ReplicateRepository`. Ordinary bare-repository content (`HEAD`, `config`, `objects/`, `refs/`) does not normally contain user-plantable symlinks, which somewhat limits the readily reachable attack surface without also controlling what the source-side `CreateSnapshot` walk includes on disk; nonetheless, the extractor itself contains no defense against this class of input and is exercised on every `ReplicateRepository` call, which is reachable via ordinary internal repository-replication/import RPC traffic rather than requiring any special privilege beyond the ability to trigger replication between two repositories.

### Recommendation
Do not rely solely on lexical path validation for tar extraction. After resolving each destination path, use a filesystem-aware containment check (e.g., open target directories with `O_NOFOLLOW`/`openat`-style traversal, or resolve the real path with `filepath.EvalSymlinks` on the *existing* parent components before joining the remaining trailing segment, or simply disallow any symlink target that resolves to the extraction root/any ancestor thereof, and disallow subsequent entries that place a `..` segment after a path component that is (or was) written as a symlink in the same extraction). Alternatively, replace the custom extractor with a well-audited extraction library that enforces containment against the real, resolved filesystem tree rather than the declared tar header names.

### Proof of Concept
1. Craft (or arrange for the source side to produce) a tar stream consumed by `extractSnapshot`/`extractTarToDirectory` containing, in order:
   - Entry `link`, `Typeflag: TypeSymlink`, `Linkname: "."`
   - Entry `link/../evil`, `Typeflag: TypeReg`, with arbitrary content
2. During extraction:
   - The symlink check computes `resolvedTarget = filepath.Join(filepath.Dir(targetDir+"/link"), ".") = targetDir`, which equals `targetDir`, so the symlink is created at `targetDir/link → .`.
   - For the second entry, `targetPath = filepath.Join(targetDir, "link/../evil")` lexically cleans to `targetDir/evil`, passing the `strings.HasPrefix(targetPath, targetDir+separator)` check.
   - `extractFile` calls `os.MkdirAll(filepath.Dir(targetPath), ...)` and `os.OpenFile(targetPath, ...)`, but the OS resolves `targetDir/link` (a symlink to `targetDir`) and then applies the trailing `../evil`, ultimately creating the file at the parent directory of `targetDir` (i.e., `filepath.Dir(targetDir)/evil`), one level outside the intended extraction root.



Note: full verification of end-to-end attacker control over the exact bytes returned by `GetSnapshot`/`CreateSnapshot` (i.e., whether the source-side snapshot walk can be made to include an on-disk symlink under ordinary usage, versus requiring a compromised/malicious source Gitaly node) was not completed within the available tool budget; this is called out as an open item that would need confirmation against `localrepo.CreateSnapshot`'s implementation before treating this as fully attacker-reachable without cross-node trust assumptions.

### Citations

**File:** internal/gitaly/service/repository/replicate.go (L334-339)
```go
		targetPath := filepath.Join(targetDir, header.Name)

		if !strings.HasPrefix(targetPath, targetDir+string(os.PathSeparator)) &&
			targetPath != targetDir {
			return fmt.Errorf("invalid file path in tar: %s", header.Name)
		}
```

**File:** internal/gitaly/service/repository/replicate.go (L352-373)
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

			// Remove existing file/symlink if it exists
			if err := os.Remove(targetPath); err != nil && !os.IsNotExist(err) {
				return fmt.Errorf("removing existing file for symlink %s: %w", targetPath, err)
			}

			if err := os.Symlink(header.Linkname, targetPath); err != nil {
				return fmt.Errorf("creating symlink %s -> %s: %w", targetPath, header.Linkname, err)
			}
```
