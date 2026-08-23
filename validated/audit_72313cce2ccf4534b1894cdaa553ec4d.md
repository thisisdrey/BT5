### Title
`VanityRepository.RelativePath` traversal bypasses backup path confinement via `WithSkipStorageExistenceCheck` - ([File: internal/gitaly/config/locator.go])

### Summary
`RestoreRepository` and `BackupRepository` validate `VanityRepository` with `locator.ValidateRepository(ctx, vanityRepo, storage.WithSkipStorageExistenceCheck())`, which returns success as soon as `RelativePath` is non-empty and never calls `storage.ValidateRelativePath` to reject `..` traversal in that mode. Since `backup.ManifestLocator` builds all backup keys (manifest, bundle, ref, and `custom_hooks_path`) via `filepath.Join(pathPrefix, storageName, relativePath, backupID, ...)` using this unsanitized `VanityRepository.RelativePath`, an attacker-influenced vanity relative path containing `../` segments can redirect these keys to another tenant's backup namespace inside the shared backup sink.

### Finding Description
`internal/gitaly/service/repository/restore_repository.go:54-72` validates the two repositories differently: [1](#0-0) 
- `in.GetRepository()` is validated with `storage.WithSkipRepositoryExistenceCheck()` only, which still goes through the full `ValidateRepository` path that calls `storage.ValidateRelativePath(storagePath, relativePath)`.
- `in.GetVanityRepository()` is validated with `storage.WithSkipStorageExistenceCheck()`.

In `internal/gitaly/config/locator.go:47-67`, when `SkipStorageExistenceCheck` is set, the function returns `nil` immediately after checking that `RelativePath` is non-empty — it never reaches the `storage.ValidateRelativePath` call that rejects `..` traversal: [2](#0-1) 

`backup.ValidatePathPrefix` (`internal/backup/backup.go:176-196`) only sanitizes the `PathPrefix` field, not `VanityRepository.RelativePath`.

`Manager.Restore` passes `req.VanityRepository` straight into `mgr.locator.Find`/`FindLatest` (`internal/backup/backup.go:404-410`), and `ManifestLocator.BeginFull`/`BeginIncremental` construct all step paths — including `CustomHooksPath` — via `filepath.Join(cfg.pathPrefix, storageName, relativePath, backupID, ...)` (`internal/backup/locator.go:40-42`, `110-114`) using the raw, unvalidated `relativePath`. `filepath.Join` collapses `..` segments, so a `RelativePath` like `../../victim-storage/victim-project.git` shifts the resolved manifest/bundle/refs/custom_hooks key out of the attacker's own `<storageName>/<relativePath>` subtree and into another repository's backup subtree within the same backup sink root.

The resulting key is then handed unmodified to `Sink.GetReader`/`GetWriter` (`internal/backup/sink.go:154-182`), which pass it straight to the underlying `blob.Bucket` — there is no additional confinement check at the sink layer tying the resolved key back to the caller's own storage/relative path.

Consequently, `mgr.restoreCustomHooks(ctx, repo, latestStep.CustomHooksPath)` (`internal/backup/backup.go:452-453`, `746-760`) reads the crafted `CustomHooksPath` and calls `repo.SetCustomHooks`, applying — to the attacker's own repository — the custom hooks archive resolved by the crafted path, which can be another tenant's `custom_hooks.tar` if the attacker can predict/guess its storage/relative path and backup ID. The same traversal also affects bundle/ref paths read during `restoreFromBundle`/`restoreFromRefs`, enabling cross-tenant content disclosure into the attacker's own repository.

### Impact Explanation
This breaks repository/tenant isolation in the backup sink: an attacker who controls the `VanityRepository.RelativePath` field of a `RestoreRepository` request can cause Gitaly to read a hooks archive (or bundle/refs) belonging to a different repository/namespace and apply it to a repository they control, disclosing another tenant's custom hooks content (which could include secrets or proprietary scripts) into their own repository. This matches "cross-repository object/metadata access" and "custom hooks disclosure" impact classes.

### Likelihood Explanation
Exploitability depends on the attacker being able to set `VanityRepository.RelativePath` on a `RestoreRepository`/`BackupRepository` call (the question's stated precondition), and on being able to predict or guess another tenant's storage name / relative path / backup ID in the shared backup sink. The `ValidatePathPrefix` check gives a false sense of confinement since it only covers `PathPrefix`, not `VanityRepository.RelativePath`, making this an easy oversight to miss during review, and the bypass condition (`WithSkipStorageExistenceCheck`) is exactly the option used on this exact request path.

### Recommendation
In `internal/gitaly/config/locator.go`'s `ValidateRepository`, always validate `RelativePath` against traversal (e.g., always call something equivalent to `storage.ValidateRelativePath` against a virtual root, or explicitly reject `..` components) regardless of `SkipStorageExistenceCheck`. Additionally, in `internal/backup/backup.go`/`internal/backup/locator.go`, explicitly validate that `VanityRepository.RelativePath` is a clean relative path without `..` segments (mirroring `backup.ValidatePathPrefix`) before using it to construct any backup sink key, and/or have `Sink.GetReader`/`GetWriter` assert that the resolved key remains within the expected `<storageName>/<relativePath>` prefix.

### Proof of Concept
```go
func TestValidateRepository_VanitySkipStorageAllowsTraversal(t *testing.T) {
    cfg := testcfg.Build(t)
    locator := config.NewLocator(cfg)

    vanity := &gitalypb.Repository{
        StorageName:  "default",
        RelativePath: "../other-tenant/victim.git",
    }

    // This currently succeeds because SkipStorageExistenceCheck short-circuits
    // before the ValidateRelativePath traversal check.
    err := locator.ValidateRepository(context.Background(), vanity, storage.WithSkipStorageExistenceCheck())
    require.NoError(t, err) // BUG: should reject traversal

    full := backupLocator.BeginFull(context.Background(), vanity, "backupid")
    // Demonstrates the resolved custom_hooks_path escapes the "default/<repo>" subtree.
    require.NotContains(t, full.Steps[0].CustomHooksPath, "default/../other-tenant")
}
```
Expected (fixed) behavior: `ValidateRepository` returns an error for the traversal path even with `WithSkipStorageExistenceCheck`, and `ValidatePathPrefix`-style checks are also applied to `VanityRepository.RelativePath` before path construction.

### Citations

**File:** internal/gitaly/service/repository/restore_repository.go (L54-69)
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
```

**File:** internal/gitaly/config/locator.go (L60-67)
```go
	relativePath := repo.GetRelativePath()
	if len(relativePath) == 0 {
		return structerr.NewInvalidArgument("%w", storage.ErrRepositoryPathNotSet)
	}

	if cfg.SkipStorageExistenceCheck {
		return nil
	}
```
