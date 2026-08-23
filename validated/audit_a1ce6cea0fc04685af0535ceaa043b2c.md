### Title
Unsanitized `BackupID` in `RestoreRepositoryRequest` allows path traversal into other repositories' backup namespace - ([File: internal/backup/manifest.go], [File: internal/backup/locator.go], [File: internal/gitaly/service/repository/restore_repository.go])

### Summary
`RestoreRepository` validates `PathPrefix` via `backup.ValidatePathPrefix` but never validates `BackupId` before passing it into `backup.RestoreRequest.BackupID`, which is later joined unsanitized with `path.Join`/`filepath.Join` to build manifest/bundle/ref paths. Because `path.Join` normalizes `..` segments, an attacker who owns a repository can supply a `BackupId` containing `../` sequences to make the manifest/bundle lookup resolve outside their own repository's backup subtree.

### Finding Description
`validateRestoreRepositoryRequest` in `internal/gitaly/service/repository/restore_repository.go` (lines 54-71) validates the attacker's `Repository`, `VanityRepository`, and `PathPrefix`, but performs no validation on `in.GetBackupId()` before it is forwarded as `backup.RestoreRequest.BackupID` [1](#0-0) .

Downstream, `ManifestLocator.Find`/`FindLatest` (`internal/backup/locator.go` lines 130-147) call into `ManifestLoader.ReadManifest`, which builds the manifest path as:
```go
manifestPath(repo, backupID, cfg.pathPrefix) // path.Join(manifestDirectory(repo, prefix), backupID+".toml")
``` [2](#0-1) 

`manifestDirectory` is scoped by `repo.GetStorageName()`/`repo.GetRelativePath()` (which are validated because `Repository` goes through `s.locator.ValidateRepository`), but `backupID` is concatenated directly with no confinement check. Since `path.Join` collapses `..` components, a `BackupId` like `../../other-repo/legit-backup-id` can walk back out of `manifests/<storageName>/<relativePath>/` and resolve into another repository's manifest path under the same backup storage root, even though `PathPrefix` itself was validated. The same unsanitized `backupID` is also used directly in `ManifestLocator.BeginFull`/`BeginIncremental` (`internal/backup/locator.go` lines 30-46, 109-118) when building `BundlePath`/`RefPath`/`CustomHooksPath` via `filepath.Join`.

`ValidatePathPrefix` only guards the `PathPrefix` field; it is never applied to `BackupID`, so the traversal-sanitizing check that exists in the code is bypassed entirely by using the `BackupId` field instead of `PathPrefix`.

### Impact Explanation
An attacker-controlled repository restore can read (and subsequently import into their own repository) another repository's backup manifest/bundle/ref-pack data by supplying a crafted `BackupId`. Because the restore writes objects into the attacker's *own* validated `Repository` target (git-write path is bound to the validated repo, not to the traversal), the primary impact is cross-repository backup data disclosure — the attacker exfiltrates another tenant's repository history/objects by "restoring" it into a repo they control and then reading it normally with git. This corresponds to a cross-tenant information disclosure / broken storage isolation bug class.

### Likelihood Explanation
The attacker only needs the ability to call `RestoreRepositoryRequest` for a repository they own (e.g., via GitLab's project restore/import flow) and knowledge or guessing of a valid `BackupId` value belonging to another repository/tenant on the same Gitaly storage (e.g., predictable timestamp-based backup IDs). This requires no special privileges, secrets, or misconfiguration — only the standard unprivileged restore capability plus the missing input validation.

### Recommendation
Validate `BackupID` (and any other locator-influencing string) with the same or a stricter version of `ValidatePathPrefix`/`storage.ValidateRelativePath` before it is used in `validateRestoreRepositoryRequest`, rejecting any value containing `..`, absolute paths, or path separators beyond a safe allowlist (e.g., timestamp-like identifiers). Additionally, harden `manifestPath`/`manifestDirectory`/`BeginFull`/`BeginIncremental` to defensively confine the joined result to remain within `manifestDirectory(repo, prefix)` (e.g., via `filepath.Rel` + prefix check) regardless of caller-supplied validation.

### Proof of Concept
```go
func TestRestoreRepository_BackupIDTraversal(t *testing.T) {
    ctx := testhelper.Context(t)
    repoA := &gitalypb.Repository{StorageName: "default", RelativePath: "attacker/repo.git"}

    locator := backup.NewLocator(sink)
    // Attacker supplies a BackupId designed to escape repoA's manifest subtree.
    maliciousBackupID := "../../victim/repo.git/2024010100000000"

    _, err := locator.Find(ctx, repoA, maliciousBackupID)
    // Expect: an error confining the lookup to repoA's own namespace.
    require.Error(t, err)
    // Actual (vulnerable) behavior: path.Join resolves outside
    // manifests/default/attacker/repo.git/, into another repo's manifest path,
    // and Find succeeds returning the victim repository's manifest.
}
```
Combined with the RPC surface: `RestoreRepositoryRequest{Repository: repoA, BackupId: "../../victim/repo.git/2024010100000000"}` reaches `validateRestoreRepositoryRequest` (which only checks `PathPrefix`, not `BackupId`) and then `manager.Restore`, demonstrating the missing confinement check described above.

### Citations

**File:** internal/gitaly/service/repository/restore_repository.go (L34-41)
```go
	err := manager.Restore(ctx, &backup.RestoreRequest{
		Repository:       in.GetRepository(),
		VanityRepository: in.GetVanityRepository(),
		AlwaysCreate:     in.GetAlwaysCreate(),
		BackupID:         in.GetBackupId(),
		UseLatest:        in.GetUseLatest(),
		PathPrefix:       in.GetPathPrefix(),
	})
```

**File:** internal/backup/manifest.go (L109-115)
```go
func manifestDirectory(repo storage.Repository, backupPathPrefix string) string {
	return path.Join(backupPathPrefix, "manifests", repo.GetStorageName(), repo.GetRelativePath())
}

func manifestPath(repo storage.Repository, backupID string, backupPathPrefix string) string {
	return path.Join(manifestDirectory(repo, backupPathPrefix), backupID+".toml")
}
```
