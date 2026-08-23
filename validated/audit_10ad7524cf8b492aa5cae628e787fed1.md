Confirmed: `cfg.Transactions.Enabled` is marked explicitly as **experimental and not production-ready** [1](#0-0) , and the code shows a non-transactional path is still fully supported and default (`internal/cli/gitaly/serve.go` sets up non-WAL middleware when `cfg.Transactions.Enabled` is false) [2](#0-1) . In that default/non-WAL mode, `objectpool.Link()` performs its "already linked" check and its file write as two separate, non-atomic steps against the plain filesystem, with no partition/WAL-level conflict detection to protect it.

### Title
Object pool linking race allows silent alternates clobbering / cross-pool corruption in non-transactional mode - (File: internal/git/objectpool/link.go)

### Summary
`Link()` first reads the repository's current alternates state via `linkedToRepository()`, and only if it decides the repository is *not* already linked does it construct a `safe.LockingFileWriter` and write the new alternates path. The "already linked" decision and the file-integrity baseline captured for the write are established at two different points in time, with a window between them in which a concurrent linking/unlinking call can alter the actual state on disk.

### Finding Description
`Link()` performs an unlocked read of the alternates file to decide behavior [3](#0-2) , calling `linkedToRepository()` which stats and reads `objects/info/alternates` without holding any lock [4](#0-3) . Only after this check returns `false` does the code create the `LockingFileWriter`: [5](#0-4) 

`safe.NewLockingFileWriter()` captures its own baseline `os.Stat()` of the target file *at construction time*, not at the time `linkedToRepository()` ran [6](#0-5) . Its later conflict checks (`Lock()` and `Commit()`) only compare against this later baseline [7](#0-6) . Consequently, if a second `Link()` (targeting a different pool) or a `DisconnectGitAlternates` call mutates the alternates file in the window between the first call's `linkedToRepository()` check and its `NewLockingFileWriter()` call, the second write is never detected as a conflict — the first writer simply adopts the new content as its "unmodified" baseline and overwrites it, discarding the other party's assignment.

This is only exploitable in Gitaly's default, non-WAL deployment mode. The transaction-manager (WAL) code path does implement proper compare-and-swap semantics for this exact scenario, as shown by the `"repository can't be linked concurrently multiple times"` test which expects a `fshistory.NewReadWriteConflictError` [8](#0-7) . However, `cfg.Transactions.Enabled` is explicitly documented as "experimental... not production ready" [1](#0-0) , meaning production Gitaly deployments run through the vulnerable non-transactional `objectpool.Link()` path directly, invoked by the `LinkRepositoryToObjectPool` RPC [9](#0-8) .

### Impact Explanation
Two repositories (or a repo and a retried request) racing to link/unlink the same member repository to different object pools can end up in an inconsistent state: the alternates file silently reflects only the last writer's pool, while housekeeping side effects from the "losing" call (e.g., `removeMemberBitmaps`, or the pool's own `FetchIntoObjectPool`/dedup bookkeeping performed under the assumption that the link succeeded) have already run against the wrong pool assignment. This can corrupt object pool membership/isolation: a repository may believe (or Rails/GitLab's tracking database may believe) it is linked to pool B, while the actual alternates file on disk points to pool A (or vice versa), leading to missing objects, unreachable data, or objects being incorrectly deduplicated against the wrong pool's contents — a storage/alternates isolation violation matching the "object-pool and alternates isolation" class in scope.

### Likelihood Explanation
`LinkRepositoryToObjectPool` and `DisconnectGitAlternates` are exposed, authenticated mutator RPCs reachable by any caller possessing the shared Gitaly auth token (the same token used for ordinary push/fetch/fork RPCs), and forking/pool operations routinely trigger these calls concurrently during real GitLab workflows (e.g., concurrent fork creation, retried RPCs, or replication races). Since production Gitaly runs with `Transactions.Enabled = false` by default, the WAL-based conflict protection does not apply, making the race directly reachable in the default configuration. The race window is small (two syscalls) but is triggerable deterministically by an attacker who can issue overlapping RPCs against the same repository/pool pair.

### Recommendation
Capture the alternates file's baseline stat once, before the `linkedToRepository()` check, and pass that identical baseline into the `LockingFileWriter` (or otherwise make the check-then-write sequence atomic under a single lock acquired before the read). Alternatively, always acquire the lock file first, then perform the "already linked" check against the locked, current state, before deciding whether to write.

### Proof of Concept
1. Create repository `R` and two object pools `A` and `B`, both valid.
2. Concurrently issue `LinkRepositoryToObjectPool(R, A)` and `LinkRepositoryToObjectPool(R, B)` against a Gitaly server running with default configuration (`Transactions.Enabled = false`).
3. Timing: Goroutine 1's `linkedToRepository()` for pool A returns `false` (unlinked); before Goroutine 1 calls `NewLockingFileWriter`, Goroutine 2 completes its full `Link()` call, writing `../../B/objects` into `R`'s alternates file and removing bitmaps assuming `B` is now authoritative.
4. Goroutine 1 then constructs its `LockingFileWriter`, which stats the file as it now stands (pointing to `B`), locks it, and overwrites it with `../../A/objects`, succeeding with no detected conflict.
5. Result: `R`'s alternates file now points to pool `A`, but pool `B`'s bitmap/dedup housekeeping already executed under the assumption that `R` was linked to `B`, leaving inconsistent state between the two pools and repository `R`.

### Citations

**File:** internal/gitaly/config/config.go (L164-168)
```go
// Transactions configures transaction related options.
type Transactions struct {
	// Enabled enables transaction support. This option is experimental
	// and intended for development only. Do not enable for other uses.
	Enabled bool `json:"enabled,omitempty" toml:"enabled,omitempty"`
```

**File:** internal/cli/gitaly/serve.go (L566-574)
```go
		}
	} else {
		storagePaths := make([]string, len(cfg.Storages))
		for i := range cfg.Storages {
			storagePaths[i] = cfg.Storages[i].Path
		}
		// When transactions are disabled, only recover pending transactions
		// if explicitly required.
		if cfg.Transactions.RecoverPendingWal {
```

**File:** internal/git/objectpool/link.go (L28-52)
```go
func Link(ctx context.Context, pool, repo *localrepo.Repo, txManager transaction.Manager) (returnedErr error) {
	altPath, err := repo.InfoAlternatesPath(ctx)
	if err != nil {
		return err
	}

	expectedRelPath, err := getRelativeObjectPath(ctx, pool, repo)
	if err != nil {
		return err
	}

	linked, err := linkedToRepository(ctx, pool, repo)
	if err != nil {
		return err
	}

	if linked {
		// When the repository is already linked to the repository, cast a vote to ensure the
		// repository is consistent with the other replicas.
		if err := transaction.VoteOnContext(ctx, txManager, voting.VoteFromData([]byte("repository linked")), voting.Synchronized); err != nil {
			return fmt.Errorf("vote on linked repository: %w", err)
		}

		return nil
	}
```

**File:** internal/git/objectpool/link.go (L54-70)
```go
	alternatesWriter, err := safe.NewLockingFileWriter(altPath)
	if err != nil {
		return fmt.Errorf("creating alternates writer: %w", err)
	}
	defer func() {
		if err := alternatesWriter.Close(); err != nil && returnedErr == nil {
			returnedErr = fmt.Errorf("closing alternates writer: %w", err)
		}
	}()

	if _, err := io.WriteString(alternatesWriter, expectedRelPath); err != nil {
		return fmt.Errorf("writing alternates: %w", err)
	}

	if err := transaction.CommitLockedFile(ctx, txManager, alternatesWriter); err != nil {
		return fmt.Errorf("committing alternates: %w", err)
	}
```

**File:** internal/git/objectpool/link.go (L169-204)
```go
func linkedToRepository(ctx context.Context, pool, repo *localrepo.Repo) (bool, error) {
	poolPath, err := pool.Path(ctx)
	if err != nil {
		return false, fmt.Errorf("getting object pool path: %w", err)
	}

	repoPath, err := repo.Path(ctx)
	if err != nil {
		return false, fmt.Errorf("getting repo path: %w", err)
	}

	altInfo, err := stats.AlternatesInfoForRepository(repoPath)
	if err != nil {
		return false, fmt.Errorf("getting alternates info: %w", err)
	}

	if !altInfo.Exists || len(altInfo.ObjectDirectories) == 0 {
		return false, nil
	}

	relPath := altInfo.ObjectDirectories[0]
	expectedRelPath, err := getRelativeObjectPath(ctx, pool, repo)
	if err != nil {
		return false, err
	}

	if relPath == expectedRelPath {
		return true, nil
	}

	if filepath.Clean(relPath) != filepath.Join(poolPath, "objects") {
		return false, fmt.Errorf("unexpected alternates content: %q", relPath)
	}

	return false, nil
}
```

**File:** internal/safe/locking_file_writer.go (L44-85)
```go
// NewLockingFileWriter creates a new LockingFileWriter for the given path. At creation, it
// stats the target file and caches its current size and last modification time such that it can
// compare on commit whether the file has changed.
func NewLockingFileWriter(path string, optionalCfg ...LockingFileWriterConfig) (*LockingFileWriter, error) {
	var cfg LockingFileWriterConfig
	if len(optionalCfg) == 1 {
		cfg = optionalCfg[0]
	} else if len(optionalCfg) > 1 {
		return nil, fmt.Errorf("locking file writer created with more than one config")
	}

	targetFile, err := os.Open(path)
	if err != nil && !os.IsNotExist(err) {
		return nil, fmt.Errorf("opening target file: %w", err)
	}
	defer targetFile.Close()

	var targetFileInfo os.FileInfo
	if targetFile != nil {
		targetFileInfo, err = targetFile.Stat()
		if err != nil {
			return nil, fmt.Errorf("statting target file: %w", err)
		}
	}

	writer, err := NewFileWriter(path, cfg.FileWriterConfig)
	if err != nil {
		return nil, fmt.Errorf("creating file writer: %w", err)
	}

	if targetFile != nil && cfg.SeedContents {
		_, err := io.Copy(writer, targetFile)
		if err != nil {
			return nil, fmt.Errorf("seeding file writer: %w", err)
		}
	}

	return &LockingFileWriter{
		writer: writer,
		fi:     targetFileInfo,
	}, nil
}
```

**File:** internal/safe/locking_file_writer.go (L126-212)
```go
func (fw *LockingFileWriter) Lock() error {
	if fw.state != lockingFileWriterStateOpen {
		return fmt.Errorf("file writer not lockable")
	}

	if err := fw.checkConcurrentModification(); err != nil {
		return err
	}

	lock, err := os.OpenFile(fw.lockPath(), os.O_CREATE|os.O_EXCL|os.O_RDONLY, mode.File)
	if err != nil {
		if os.IsExist(err) {
			return ErrFileAlreadyLocked
		}

		return fmt.Errorf("creating lock file: %w", err)
	}
	_ = lock.Close()

	fw.state = lockingFileWriterStateLocked

	return nil
}

func (fw *LockingFileWriter) unlock() error {
	// We only want to unlock in case we have locked this file ourselves. Otherwise, we risk
	// removing the lock from another, concurrent locking file writer.
	if fw.state != lockingFileWriterStateLocked {
		return fmt.Errorf("file writer not locked")
	}

	if err := os.Remove(fw.lockPath()); err != nil {
		return fmt.Errorf("removing lock file: %w", err)
	}

	fw.state = lockingFileWriterStateClosed

	return nil
}

// Commit writes whatever has been written to the Filewriter to the target file if and only if the
// target file has not been modified meanwhile. The writer must be `Lock()`ed first. The writer
// will be closed after this call, with all locks and temporary files having been removed.
func (fw *LockingFileWriter) Commit(ctx context.Context) (returnedErr error) {
	if fw.state != lockingFileWriterStateLocked {
		return fmt.Errorf("file writer not locked")
	}

	// While we have already checked that there was no concurrent modification when locking the
	// file, we do so again here in order to verify that no other processes which are unaware of
	// the locking semantics have changed the file. This may be overly cautious, but on the
	// other hand the single stat(3P) call shouldn't be all that expensive in the first place.
	if err := fw.checkConcurrentModification(); err != nil {
		return err
	}

	if err := fw.writer.Commit(ctx); err != nil {
		return fmt.Errorf("committing file: %w", err)
	}

	if err := fw.unlock(); err != nil {
		return fmt.Errorf("unlocking file: %w", err)
	}

	return nil
}

func (fw *LockingFileWriter) checkConcurrentModification() error {
	fi, err := os.Stat(fw.writer.path)
	if err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("statting path: %w", err)
	}

	if fw.fi == nil && fi != nil {
		return fmt.Errorf("file concurrently created")
	}
	if fw.fi != nil && fi == nil {
		return fmt.Errorf("file concurrently deleted")
	}
	if fw.fi != nil && fi != nil {
		if fw.fi.Size() != fi.Size() || fw.fi.ModTime() != fi.ModTime() || fw.fi.Mode() != fi.Mode() {
			return fmt.Errorf("file concurrently modified")
		}
	}

	return nil
}
```

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager_alternate_test.go (L644-689)
```go
			desc: "repository can't be linked concurrently multiple times",
			steps: steps{
				RemoveRepository{},
				StartManager{},
				Begin{
					TransactionID: 1,
					RelativePaths: []string{"pool"},
				},
				CreateRepository{
					TransactionID: 1,
				},
				Commit{
					TransactionID: 1,
				},
				Begin{
					TransactionID:       2,
					RelativePaths:       []string{"member"},
					ExpectedSnapshotLSN: 1,
				},
				CreateRepository{
					TransactionID: 2,
				},
				Commit{
					TransactionID: 2,
				},
				Begin{
					TransactionID:       3,
					RelativePaths:       []string{"member", "pool"},
					ExpectedSnapshotLSN: 2,
				},
				Begin{
					TransactionID:       4,
					RelativePaths:       []string{"member", "pool"},
					ExpectedSnapshotLSN: 2,
				},
				Commit{
					TransactionID:   3,
					UpdateAlternate: &alternateUpdate{RelativePath: "pool"},
				},
				Commit{
					TransactionID:   4,
					UpdateAlternate: &alternateUpdate{RelativePath: "pool"},
					ExpectedError: fshistory.NewReadWriteConflictError(
						filepath.Join("member", "objects", "info", "alternates"), 2, 3,
					),
				},
```

**File:** proto/objectpool.proto (L52-58)
```text
  // LinkRepositoryToObjectPool links the specified repository to the object pool. Objects contained
  // in the object pool will be deduplicated for this repository when repacking objects.
  rpc LinkRepositoryToObjectPool(LinkRepositoryToObjectPoolRequest) returns (LinkRepositoryToObjectPoolResponse) {
    option (op_type) = {
      op: MUTATOR
    };
  }
```
