### Title
Alternate-derived path in `createRepositorySnapshots` is never validated to stay within `storageRoot`, allowing storage-path escape - (File: internal/gitaly/storage/storagemgr/partition/snapshot/snapshot.go)

### Summary
`createRepositorySnapshots` validates the caller-provided `relativePath` with `storage.ValidateGitDirectory` before snapshotting it, but the alternate path it derives from the repository's own `objects/info/alternates` file (`alternateRelativePath`) is used to build both the source and destination paths for the snapshot with no bounds check at all. This is inconsistent with the equivalent code in `partition_assigner.go`, which explicitly wraps the identical computation in `storage.ValidateRelativePath`.

### Finding Description
In `createRepositorySnapshots` [1](#0-0) , once the primary repository at `relativePath` is confirmed to be a valid git directory via `storage.ValidateGitDirectory` [2](#0-1) , the code reads that repository's `objects/info/alternates` file and computes:

```go
alternateRelativePath := filepath.Dir(filepath.Join(relativePath, "objects", alternate))
```

`alternate` is the literal content of the attacker's own `objects/info/alternates` file. Unlike the identical computation in `partition_assigner.go`'s `getAlternatePartitionID`, which is immediately passed through `storage.ValidateRelativePath` to guarantee the resolved path stays under `storagePath` [3](#0-2) , the snapshot code performs no such check. It proceeds directly to:

```go
createParentDirectories(storageRoot, snapshotRoot, alternateRelativePath, stats)
createRepositorySnapshot(ctx, storageRoot, snapshotRoot, alternateRelativePath, snapshotFilter, stats)
```

`createRepositorySnapshot` then joins `alternateRelativePath` onto both `storageRoot` (read side) and `snapshotRoot` (write side) via `filepath.Join` [4](#0-3) . `filepath.Join` cleans `..` segments, so if the alternates content contains extra `../` segments, `filepath.Join(storageRoot, alternateRelativePath)` can resolve to a directory outside `storageRoot`, and `filepath.Join(snapshotRoot, alternateRelativePath)` resolves outside `snapshotRoot` by the same offset. Critically, `storage.ValidateGitDirectory` — the mechanism the code comments cite as the sole protection against snapshotting non-leaf/out-of-tree directories ("we achieve this protection by only snapshotting valid Git directories") — is only applied to the original `relativePath`, never to `alternateRelativePath`. `createDirectorySnapshot` then walks the escaped source directory and recreates its structure (via `os.Mkdir`) and hard-links its regular files (via `os.Link`) at the escaped destination path, with no additional validation.

### Impact Explanation
This is a storage-path escape / cross-repository disclosure bug: an out-of-tree or another tenant's directory can be pulled into a transactional WAL snapshot and its file contents hard-linked into a path the attacker's subsequent operations on the transaction (e.g. `GetSnapshot`) can read, disclosing another repository's or another storage's object/file content. The write side is also affected since the destination join uses the same unvalidated relative offset, meaning directories/hard-links can be created outside the intended `snapshotRoot`, subject to on-disk directory availability and filesystem permissions.

### Likelihood Explanation
Per the stated precondition, the attacker controls the content of their own repository's `objects/info/alternates` file and WAL/transactional storage is enabled. Triggering `createRepositorySnapshots` only requires initiating a transaction/snapshot on that repository (e.g. any write RPC or `GetSnapshot`). No admin privilege, shell access, or peer compromise is required beyond what is already granted by the question's precondition. The exploit is deterministic and repeatable once the alternates content is in place.

### Recommendation
Validate `alternateRelativePath` the same way `partition_assigner.go` does, e.g.:
```go
alternateRelativePath, err := storage.ValidateRelativePath(storageRoot, filepath.Dir(filepath.Join(relativePath, "objects", alternate)))
if err != nil {
    return fmt.Errorf("validate alternate relative path: %w", err)
}
```
before using it in `createParentDirectories`/`createRepositorySnapshot`, and additionally confirm the alternate resolves to a distinct git directory (mirroring the `ErrAlternatePointsToSelf` check) rather than relying implicitly on `ValidateGitDirectory` alone.

### Proof of Concept
```go
func TestCreateRepositorySnapshots_alternateEscapesStorageRoot(t *testing.T) {
    ctx := testhelper.Context(t)
    storageRoot := t.TempDir()
    snapshotRoot := filepath.Join(storageRoot, ".snapshot")

    // Secret directory outside storageRoot that should never be exposed.
    secretDir := t.TempDir()
    require.NoError(t, os.WriteFile(filepath.Join(secretDir, "secret.txt"), []byte("top secret"), 0o644))

    // Victim-controlled repo with a crafted alternates file pointing outside storageRoot.
    repoRelPath := "@hashed/aa/bb/repo.git"
    repoPath := filepath.Join(storageRoot, repoRelPath)
    require.NoError(t, os.MkdirAll(filepath.Join(repoPath, "objects", "info"), 0o755))
    require.NoError(t, os.MkdirAll(filepath.Join(repoPath, "refs"), 0o755))
    require.NoError(t, os.WriteFile(filepath.Join(repoPath, "HEAD"), []byte("ref: refs/heads/main\n"), 0o644))

    // Crafted alternate: enough "../" to escape storageRoot into secretDir.
    relToSecret, err := filepath.Rel(filepath.Join(repoPath, "objects"), secretDir)
    require.NoError(t, err)
    require.NoError(t, os.WriteFile(
        filepath.Join(repoPath, "objects", "info", "alternates"),
        []byte(relToSecret), 0o644,
    ))

    var stats snapshotStatistics
    err = createRepositorySnapshots(ctx, storageRoot, snapshotRoot, []string{repoRelPath}, AllFilter(), &stats)

    // Expected (fixed) behavior: escape should be rejected.
    require.Error(t, err)

    // Observed (vulnerable) behavior: no error, and secret.txt is linked into the snapshot,
    // e.g. under snapshotRoot/../../<...>/secret.txt, demonstrating disclosure.
}
```

### Citations

**File:** internal/gitaly/storage/storagemgr/partition/snapshot/snapshot.go (L160-172)
```go
		if err := storage.ValidateGitDirectory(filepath.Join(storageRoot, relativePath)); err != nil {
			if errors.Is(err, fs.ErrNotExist) {
				// It's okay if the repository does not exist. We'll create a snapshot without the directory,
				// and the RPC handlers can handle the situation as best fit.
				continue
			}

			// The transaction logic doesn't require the snapshotted repository to be valid. We want to ensure
			// we only snapshot a 'leaf'/project directories in the storage. Otherwise relative paths like
			// `@hashed/xx` could attempt to snapshot an entire subtree. As Gitaly doesn't control the directory
			// hierarchy yet, we achieve this protection by only snapshotting valid Git directories.
			return fmt.Errorf("validate git directory: %w", err)
		}
```

**File:** internal/gitaly/storage/storagemgr/partition/snapshot/snapshot.go (L180-209)
```go
		// Read the repository's 'objects/info/alternates' file to figure out whether it is connected
		// to an alternate. If so, we need to include the alternate repository in the snapshot along
		// with the repository itself to ensure the objects from the alternate are also available.
		if alternate, err := gitstorage.ReadAlternatesFile(filepath.Join(snapshotRoot, relativePath)); err != nil && !errors.Is(err, gitstorage.ErrNoAlternate) {
			return fmt.Errorf("get alternate path: %w", err)
		} else if alternate != "" {
			// The repository had an alternate. The path is a relative from the repository's 'objects' directory
			// to the alternate's 'objects' directory. Build the relative path of the alternate repository.
			alternateRelativePath := filepath.Dir(filepath.Join(relativePath, "objects", alternate))
			if _, ok := snapshottedRepositories[alternateRelativePath]; ok {
				continue
			}

			if err := createParentDirectories(storageRoot, snapshotRoot, alternateRelativePath, stats); err != nil {
				return fmt.Errorf("create parent directories: %w", err)
			}

			// Include the alternate repository in the snapshot as well.
			if err := createRepositorySnapshot(ctx,
				storageRoot,
				snapshotRoot,
				alternateRelativePath,
				snapshotFilter,
				stats,
			); err != nil {
				return fmt.Errorf("create alternate snapshot: %w", err)
			}

			snapshottedRepositories[alternateRelativePath] = struct{}{}
		}
```

**File:** internal/gitaly/storage/storagemgr/partition/snapshot/snapshot.go (L262-271)
```go
func createRepositorySnapshot(ctx context.Context, storageRoot, snapshotRoot, relativePath string,
	snapshotFilter Filter, stats *snapshotStatistics,
) error {
	if err := createDirectorySnapshot(ctx, filepath.Join(storageRoot, relativePath),
		filepath.Join(snapshotRoot, relativePath),
		snapshotFilter, stats); err != nil {
		return fmt.Errorf("create directory snapshot: %w", err)
	}
	return nil
}
```

**File:** internal/gitaly/storage/storagemgr/partition_assigner.go (L330-342)
```go
	// The relative path should point somewhere within the same storage.
	alternateRelativePath, err := storage.ValidateRelativePath(
		pa.storagePath,
		// Take the relative path to the repository, not 'repository/objects'.
		filepath.Dir(
			// The path in alternates file points to the object directory of the alternate
			// repository. The path is relative to the repository's own object directory.
			filepath.Join(relativePath, "objects", alternate),
		),
	)
	if err != nil {
		return 0, fmt.Errorf("validate relative path: %w", err)
	}
```
