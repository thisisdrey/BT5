### Title
Missing validation of `BackupId` allows path traversal in backup manifest/object lookup - ([File: internal/gitaly/service/repository/restore_repository.go])

### Summary
`validateRestoreRepositoryRequest` validates `Repository`, `VanityRepository` (via `storage.ValidateRelativePath` inside `ValidateRepository`) and `PathPrefix` (via `backup.ValidatePathPrefix`), but it never validates `BackupId` [1](#0-0) . `BackupId` is then joined unsanitized into sink object keys via `path.Join`/`filepath.Join` in `manifest.go` and `locator.go`, allowing `..` traversal to escape the intended `manifests/<storage>/<relativePath>/` subtree.

### Finding Description
`RestoreRepository` forwards `in.GetBackupId()` straight into `backup.RestoreRequest.BackupID` [2](#0-1) , which eventually reaches `ManifestLocator.Find` → `ManifestLoader.ReadManifest` → `manifestPath(repo, backupID, prefix)`:
```
func manifestPath(repo storage.Repository, backupID string, backupPathPrefix string) string {
	return path.Join(manifestDirectory(repo, backupPathPrefix), backupID+".toml")
}
``` [3](#0-2) 

`path.Join` lexically collapses `..` segments, so a `backupID` such as `../../other-repo/abc` resolves the final key outside the `manifests/<storageName>/<relativePath>/` directory that is otherwise enforced by validating `Repository`'s storage name/relative path. The same unsanitized `backupID` is also used directly in `ManifestLocator.BeginFull`/`BeginIncremental` to build `filepath.Join(cfg.pathPrefix, storageName, relativePath, backupID, "001.bundle")` [4](#0-3) , so the same escape applies to bundle, ref, and custom_hooks object paths.

While `Repository`/`VanityRepository` fields are checked through `s.locator.ValidateRepository` (which internally applies `storage.ValidateRelativePath`) and `PathPrefix` goes through `backup.ValidatePathPrefix`, there is no equivalent check on `BackupId` anywhere in `validateRestoreRepositoryRequest` or in `ManifestLocator`/`ManifestLoader`. Because `BackupId` is a free-form string fully controlled by the caller of `RestoreRepositoryRequest`, this is a straightforward unsanitized-path-component bug feeding a `path.Join` call whose output is used as an object-storage key in `Sink.GetReader`.

### Impact Explanation
An attacker who can invoke `RestoreRepository` for a repository they control can supply a crafted `BackupId` to make `ManifestLocator.Find`/`BeginFull` resolve to a manifest/bundle path belonging to another repository (e.g. a different `relativePath` under the same storage, if the attacker knows or guesses it), then have that content restored into their own repository. This matches the "cross-repository objects or metadata disclosed" impact class — reading another tenant's committed bundle/refs/custom_hooks via the backup subsystem and materializing it in a repository the attacker controls.

### Likelihood Explanation
Precondition is that server-side backups are configured (`s.backupSink`/`s.backupLocator` non-nil) — a standard, supported Gitaly deployment configuration, not a special/insecure one. The attacker only needs the ability to call `RestoreRepository` on a `Repository`/`VanityRepository` they own, which is within the stated unprivileged-attacker capability set. The exploit requires guessing or knowing another repository's `relativePath`/`backupID` naming (backup IDs are often predictable timestamps), which affects exploitability but not the presence of the missing-validation bug itself.

### Recommendation
Validate `BackupId` (and any other path-component-derived fields, e.g. via `storage.ValidateRelativePath` or a dedicated allowlist regex rejecting `/`, `\`, and `..`) inside `validateRestoreRepositoryRequest` before it is passed to `backup.RestoreRequest`, mirroring the existing checks for `Repository`/`VanityRepository`/`PathPrefix`. Additionally, harden `manifestPath`/`manifestDirectory`/`ManifestLocator.BeginFull`/`BeginIncremental` to reject any `backupID` value that is not a "clean" single path segment (no `..`, no path separators) before using it in `path.Join`/`filepath.Join`.

### Proof of Concept
```go
// internal/backup/manifest_test.go (illustrative)
func TestManifestPath_BackupIDTraversal(t *testing.T) {
	repo := &gitalypb.Repository{StorageName: "default", RelativePath: "victim-tenant/repo.git"}
	// Attacker-controlled backupID with traversal
	p := manifestPath(repo, "../../attacker-tenant/other-repo/abc", "")
	// Expect: p stays within "manifests/default/victim-tenant/repo.git/"
	require.True(t, strings.HasPrefix(p, "manifests/default/victim-tenant/repo.git/"),
		"backupID escaped intended manifest directory: %s", p)
}
```
Expected today: the assertion fails because `path.Join` collapses the `../../` segments, producing a key like `manifests/default/attacker-tenant/other-repo/abc.toml` outside the `victim-tenant/repo.git` subtree — demonstrating the traversal is unmitigated. An end-to-end RPC PoC would call `RestoreRepository` with `BackupId: "../../other-repo/abc"` targeting a sink that has another repository's backup at that guessed path, and confirm the attacker's repository is populated with the other repo's bundle content.

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

**File:** internal/gitaly/service/repository/restore_repository.go (L54-71)
```go
func (s *server) validateRestoreRepositoryRequest(ctx context.Context, in *gitalypb.RestoreRepositoryRequest) error {
	if err := s.locator.ValidateRepository(ctx, in.GetRepository(),
		storage.WithSkipRepositoryExistenceCheck(),
	); err != nil {
		return fmt.Errorf("repository: %w", err)
	}

	if err := s.locator.ValidateRepository(ctx, in.GetVanityRepository(),
		storage.WithSkipStorageExistenceCheck(),
	); err != nil {
		return fmt.Errorf("vanity repository: %w", err)
	}

	if err := backup.ValidatePathPrefix(in.GetPathPrefix()); err != nil {
		return err
	}

	return nil
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

**File:** internal/backup/locator.go (L30-46)
```go
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
