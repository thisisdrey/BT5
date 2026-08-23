### Title
Unsanitized `BackupId` in `RestoreRepository` allows backup-path traversal into another repository's custom hooks archive - (File: internal/gitaly/service/repository/restore_repository.go)

### Summary
`RestoreRepository` validates `Repository`, `VanityRepository`, and `PathPrefix` before calling `backup.Manager.Restore`, but never validates `BackupId` for path-traversal sequences. `BackupId` is passed straight into `filepath.Join` calls in `ManifestLocator.BeginFull`/`BeginIncremental`/`Find`, which build the `CustomHooksPath` (and bundle/ref paths) that `restoreCustomHooks` later reads from the shared backup sink.

### Finding Description
`validateRestoreRepositoryRequest` in `internal/gitaly/service/repository/restore_repository.go:54-72` only validates `Repository`, `VanityRepository` (via `s.locator.ValidateRepository`, which rejects `..` in relative paths) and `PathPrefix` (via `backup.ValidatePathPrefix`). `BackupId` (`in.GetBackupId()`) is passed unvalidated into `backup.RestoreRequest{BackupID: in.GetBackupId()}` at line 38 [1](#0-0) .

Inside `Manager.Restore` (`internal/backup/backup.go:388-454`), `req.BackupID` is forwarded to `mgr.locator.Find(ctx, req.VanityRepository, req.BackupID, ...)` [2](#0-1) . `ManifestLocator.BeginFull` and `BeginIncremental` build the on-disk/object-storage keys with `filepath.Join(cfg.pathPrefix, storageName, relativePath, backupID, "...")`, using the raw, unsanitized `backupID` string as a path segment [3](#0-2) . Because `filepath.Join` cleans `..` segments lexically, a `BackupId` value such as `../../other-storage/other/relative/path/other-backup-id` collapses the resulting key to point at a completely different `storageName/relativePath/backupID` triplet than the one the caller was validated against, letting the resolved `CustomHooksPath` land in another tenant's backup namespace inside the same shared `Sink`. The final restore step, `mgr.restoreCustomHooks(ctx, repo, latestStep.CustomHooksPath)` at `internal/backup/backup.go:452-453`, then extracts whatever tar exists at that (attacker-influenced) key into the caller's own repository, without any check that the resolved path stays under the caller's own `storageName/relativePath` prefix [4](#0-3) .

The existing defenses — `s.locator.ValidateRepository` for `Repository`/`VanityRepository` and `backup.ValidatePathPrefix` for `PathPrefix` — do not cover `BackupID`, so nothing stops `..`-laden identifiers from escaping the intended per-repository backup subdirectory.

### Impact Explanation
An attacker who owns a repository and can invoke `RestoreRepository` (self-service disaster-recovery / restore flow) can supply a crafted `BackupId` to make Gitaly extract another repository's custom hooks tarball (or an attacker-forged object at a guessed/known key) into their own repository's hooks directory. This is a cross-tenant object-storage read plus hook injection primitive — the attacker gains control of hook scripts without ever calling `SetCustomHooks` directly on the victim repository, which matches GitLab's "hook or quarantine bypass" / "cross-repository object access" impact classes.

### Likelihood Explanation
Exploitation requires the attacker to be able to call `RestoreRepository` with server-side backups configured (already assumed reachable per the audit scope) and to know or guess the victim's `storageName`/`relativePath`/`backupID` triplet stored in the shared backup sink. Relative paths for GitLab-hosted repos are often derived deterministically (hashed storage paths), and backup IDs may be predictable/sequential, which lowers — but does not eliminate — the guessing difficulty. The lack of any sanitization on `BackupID` means the traversal itself is trivially reproducible once a target key is known.

### Recommendation
Validate `BackupId` (and re-validate the final joined key) the same way `PathPrefix` is validated — reject any segment containing `..`, path separators beyond a safe charset, or absolute-path indicators — before passing it into `ManifestLocator`. Additionally, after computing `CustomHooksPath`/`BundlePath`/`RefPath` in `ManifestLocator.BeginFull`/`BeginIncremental`, assert (e.g. via `storage.ValidateRelativePath` or an explicit prefix check) that the resulting cleaned path still begins with `filepath.Join(cfg.pathPrefix, storageName, relativePath)` before using it to read/write from the sink.

### Proof of Concept
```go
func TestRestoreRepository_BackupIDTraversal(t *testing.T) {
    // Set up sink with two "tenants": victim repo backup with malicious custom hooks,
    // and attacker repo with no backup of its own.
    // victimKey := "manifests/storage/@hashed/victim/repo.git/victim-backup.toml"
    // attackerRelativePath resolves to a different repo.

    req := &gitalypb.RestoreRepositoryRequest{
        Repository:       attackerRepo,
        VanityRepository: attackerRepo,
        BackupId:         "../../@hashed/victim/repo.git/victim-backup",
    }

    _, err := client.RestoreRepository(ctx, req)
    // Expected (secure) behavior: request fails with InvalidArgument due to
    // path traversal detection in BackupId.
    // Actual (vulnerable) behavior: succeeds, and attackerRepo's
    // custom_hooks directory now contains victim's hook scripts.
    require.Error(t, err)
}
```
The test should assert that Gitaly rejects the `BackupId` before resolving any path, and that `restoreCustomHooks` never reads a `CustomHooksPath` outside `filepath.Join(pathPrefix, attackerRepo.StorageName, attackerRepo.RelativePath)`.

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

**File:** internal/backup/backup.go (L403-404)
```go
	if req.BackupID != "" {
		backup, err = mgr.locator.Find(ctx, req.VanityRepository, req.BackupID, locatorOpts...)
```

**File:** internal/backup/backup.go (L450-453)
```go
	// Restore custom hooks. Each custom hooks archive contains the entirety of the hooks, so
	// we can just restore the most recent archive.
	latestStep := backup.Steps[len(backup.Steps)-1]
	return mgr.restoreCustomHooks(ctx, repo, latestStep.CustomHooksPath)
```

**File:** internal/backup/locator.go (L29-46)
```go
// BeginFull returns a tentative first step needed to create a new full backup.
func (l ManifestLocator) BeginFull(ctx context.Context, repo storage.Repository, backupID string, opts ...Option) *Backup {
	cfg := applyOptions(opts)
	storageName := repo.GetStorageName()
	relativePath := repo.GetRelativePath()

	return &Backup{
		ID:         backupID,
		Repository: repo,
		Steps: []Step{
			{
				BundlePath:      filepath.Join(cfg.pathPrefix, storageName, relativePath, backupID, "001.bundle"),
				RefPath:         filepath.Join(cfg.pathPrefix, storageName, relativePath, backupID, "001.refs"),
				CustomHooksPath: filepath.Join(cfg.pathPrefix, storageName, relativePath, backupID, "001.custom_hooks.tar"),
			},
		},
	}
}
```
