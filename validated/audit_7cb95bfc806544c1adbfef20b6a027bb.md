Confirmed: `AlternatesInfoForRepository` (`internal/git/stats/repository_info.go:647-668`) reads `objects/info/alternates` verbatim via `ReadAlternatesFile` with **no validation** that the referenced path stays inside the storage root — unlike the equivalent logic in `internal/git/dirs.go`'s `altObjectDirs` (which enforces `strings.HasPrefix(newDir, storagePrefix)`), `internal/gitaly/storage/storagemgr/partition_assigner.go`'s `getAlternatePartitionID` (which calls `storage.ValidateRelativePath`), and `internal/gitaly/service/internalgitaly/scan_pool_metadata.go` (which calls `storage.ValidateGitDirectory` before trusting the pool path). `RepositoryInfo` in `internal/gitaly/service/repository/repository_info.go:43-61` consumes this unvalidated data directly: it takes `repoInfo.Alternates.AbsoluteObjectDirectories()[0]`, derives `poolRepoPath`, and computes `poolRelativePath` via a bare `filepath.Rel(storagePath, poolRepoPath)` — with no `ValidateRelativePath`/prefix check — before building a `poolRepo` and walking/stat-ing that directory via `dirSizeInBytes` and `stats.PackfilesInfoForRepository`/`LooseObjectsInfoForRepository`.

### Title
RepositoryInfo RPC discloses size/object metadata for arbitrary filesystem paths via unvalidated `objects/info/alternates` content - (File: internal/gitaly/service/repository/repository_info.go)

### Summary
The `RepositoryInfo` RPC merges "pool" statistics into its response whenever a repository's `objects/info/alternates` file is present. The pool path is derived straight from the alternates file contents without being checked against the storage root, unlike other Gitaly code paths that read the same file.

### Finding Description
`RepositoryInfo` (`internal/gitaly/service/repository/repository_info.go:17-86`) calls `stats.RepositoryInfoForRepository`, which internally calls `AlternatesInfoForRepository` (`internal/git/stats/repository_info.go:647-668`). That function simply opens `objects/info/alternates` and returns its raw lines as `ObjectDirectories`, performing no bounds checking. `AbsoluteObjectDirectories()` (`internal/git/stats/repository_info.go:590-602`) then joins these raw, attacker-influenced strings against the repo path (for relative entries) or uses them verbatim (for absolute entries) — again with no containment check.

Back in `RepositoryInfo`, this untrusted absolute path is used directly: [1](#0-0) 
`filepath.Rel(storagePath, poolRepoPath)` does not fail or reject paths outside `storagePath`; it just produces a relative path containing `..` segments. This differs from the two other call sites that read the same alternates data and each apply an explicit escape check: `internal/git/dirs.go`'s `altObjectDirs` uses `strings.HasPrefix(newDir, storagePrefix)` and returns `alternateOutsideStorageError` otherwise, and `internal/gitaly/storage/storagemgr/partition_assigner.go`'s `getAlternatePartitionID` calls `storage.ValidateRelativePath`. `internal/gitaly/service/internalgitaly/scan_pool_metadata.go` additionally calls `storage.ValidateGitDirectory(poolRepoPath)` before trusting the pool path. `RepositoryInfo` has none of these guards.

A repository's `objects/info/alternates` file is attacker-reachable content in ordinary Gitaly workflows: it can be populated by `git commit-graph`/`git fetch` with `GIT_ALTERNATE_OBJECT_DIRECTORIES`, by direct writes during quarantine/`ReceivePack` processing, or by any RPC that persists repository state where the alternates file is not subsequently sanitized by Gitaly's own writer (`internal/git/objectpool/link.go`'s `Link` writes a controlled, safe value, but that is not the only way the file can end up populated — the file is repo-local and can pre-exist from a replicated/imported repository, e.g. via `CreateRepositoryFromBundle`/`ReplicateRepository`, before Gitaly's own linking logic ever runs).

### Impact Explanation
When `RepositoryInfo` encounters such a maliciously crafted alternates entry pointing outside the storage root (e.g. `../../../../etc` or an absolute path to another tenant's storage), it will walk that directory (`dirSizeInBytes`), read loose-object shard directories (`LooseObjectsInfoForRepository`), and read packfile directory entries (`PackfilesInfoForRepository`) under that path, then return aggregated size/count metadata to the caller. This is a cross-repository/cross-storage information-disclosure primitive: an attacker-controlled repository can be used to have Gitaly report byte-accurate size, object counts, and loose/pack metadata of directories the requesting actor should not have access to (e.g., another repository or storage location on the same node), constituting a concrete storage escape / cross-repository access analogous in mechanism to the referenced report's failure to use the correct/validated aggregate.

### Likelihood Explanation
The `RepositoryInfo` RPC is a plain `ACCESSOR` RPC (`proto/repository.proto:32-36`) reachable by any caller authorized to query a given repository. Populating a crafted `objects/info/alternates` file requires only the ability to get such a file into a repository's `objects/info` directory (e.g., through replication/import of a repository whose alternates file was never normalized, or any code path that copies repository contents verbatim without re-validating alternates before Gitaly's own tracking metadata is considered authoritative). No privileged access or malicious peer/MITM assumptions are needed beyond what an ordinary Gitaly RPC caller/pusher already has.

### Recommendation
In `internal/gitaly/service/repository/repository_info.go`, validate the derived pool path the same way other call sites do before using it: apply `storage.ValidateRelativePath(storagePath, poolRepoPath)` (or reuse `git.AlternateObjectDirectories`, which already enforces the storage-root prefix check) instead of a bare `filepath.Rel`, and reject/skip the pool-merge step (returning a structured error, not silently truncating) if the alternate resolves outside the storage root, mirroring the existing protection in `internal/git/dirs.go` and `internal/gitaly/storage/storagemgr/partition_assigner.go`.

### Proof of Concept
Not independently reproduced in the index (would require running Gitaly with a repository whose `objects/info/alternates` file is written, e.g. via replication of a crafted archive, to contain `../../../victim-storage/victim-repo/objects`, then calling `RepositoryService.RepositoryInfo` on the attacker repository and observing that the response's `Size`/`Objects` fields reflect data read from the victim path). This should be validated by starting a Devin session with full repository access to confirm reachability of writing such an alternates file end-to-end and to build a concrete regression test, since the exact code path that persists an unvalidated alternates file prior to `Link()` being called could not be fully traced within the indexed context.

### Citations

**File:** internal/gitaly/service/repository/repository_info.go (L45-56)
```go
	if repoInfo.Alternates.Exists && len(repoInfo.Alternates.AbsoluteObjectDirectories()) > 0 {
		poolRepoPath := filepath.Dir(repoInfo.Alternates.AbsoluteObjectDirectories()[0])

		storagePath, err := s.locator.GetStorageByName(ctx, request.GetRepository().GetStorageName())
		if err != nil {
			return nil, fmt.Errorf("getting storage path: %w", err)
		}

		poolRelativePath, err := filepath.Rel(storagePath, poolRepoPath)
		if err != nil {
			return nil, fmt.Errorf("computing pool relative path: %w", err)
		}
```
