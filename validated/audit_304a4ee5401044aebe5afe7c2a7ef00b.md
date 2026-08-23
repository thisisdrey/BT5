### Title
DisconnectGitAlternates leaves repository permanently broken with no automated recovery if Gitaly crashes mid-operation - (File: internal/git/objectpool/disconnect.go)

### Summary
`ObjectPoolService.DisconnectGitAlternates` unlinks a repository from its object pool by hard-linking pool objects into the member repository, then renaming away `objects/info/alternates` and running a connectivity check before permanently discarding the backup. If Gitaly crashes between the rename and the point where the backup is safely restored or discarded, the repository is left in a broken, inaccessible state that only a human administrator can repair by manually restoring the backed-up alternates file — there is no automated recovery path, exactly mirroring the reported bug class of "a revert/rollback path that itself fails, permanently losing/locking the resource with no on-chain/automatic remediation."

### Finding Description
`Disconnect()` performs a multi-step, non-atomic operation on disk:
1. Hard-link every object from the alternate object directory into the repository's own `objects` directory [1](#0-0) .
2. Call `removeAlternatesIfOk`, which renames `objects/info/alternates` to a timestamped backup file, then runs `git rev-list --objects --all --quiet` to verify the repository is still fully connected without the alternates link [2](#0-1) .
3. If the connectivity check fails, a `defer`-registered rollback copies the backup file back over `objects/info/alternates` [3](#0-2) .

The function's own doc comment states the risk explicitly: *"if Gitaly crashes in the middle of this function, the repo is left in a broken state. We do take care to leave a copy of the alternates file, so that it can be manually restored by an administrator if needed."* [4](#0-3)  The exported RPC-level wrapper repeats the same warning: *"If we are very unlucky and Gitaly crashes, the repository stays in a broken state until an administrator intervenes and restores the backed-up copy of objects/info/alternates."* [5](#0-4) 

Because the rollback logic lives entirely in an in-memory `defer` closure, it cannot execute if the process dies (crash, OOM-kill, forced restart) after the rename but before the closure runs. At that point `objects/info/alternates` is gone, the repository can no longer resolve objects that live only in the pool, and every RPC against that repository fails until an operator manually copies the `*.timestamp.hexsuffix` backup file back into place — there is no code path that automatically detects and repairs this state on the next startup or RPC.

### Impact Explanation
This is directly analogous to the reported bug class: an operation intended to safely "revert"/undo a linkage (disconnect from pool, restoring self-sufficiency) can itself fail partway through in a way that is unrecoverable by the system itself, permanently degrading (locking) the resource — here, the entire repository becomes unusable (all RPCs against it fail, i.e., a full denial of service for that repository) — until a privileged administrator manually restores state from a backup file left on disk. This is the same shape as "funds lost/locked because the revert-failure path has no automatic remedy," just manifesting as repository-availability loss rather than token loss. `DisconnectGitAlternates` is a normal, non-privileged-actor code path invoked during routine object-pool lifecycle management (fork network maintenance) and Praefect replication reconciliation [6](#0-5) , not something that requires a malicious peer, leaked token, or MITM.

### Likelihood Explanation
Triggering the underlying crash-at-the-wrong-moment condition requires an external event (process crash, OOM kill, forced Gitaly restart, node failure) coinciding with the narrow window between the `os.Rename` and rollback/commit completion in `removeAlternatesIfOk`. This is a realistic operational occurrence (Gitaly nodes are killed/restarted routinely by orchestrators such as Kubernetes, and by Praefect during failover), but it is not attacker-triggerable on demand — likelihood is moderate and tied to environmental instability rather than to a crafted malicious request.

### Recommendation
- Make the alternates removal/restoration crash-safe, e.g. by recording pending disconnect operations in a durable, crash-recoverable log (similar to the transactional WAL machinery already used elsewhere in Gitaly, see `internal/gitaly/storage/storagemgr/partition/transaction_manager.go`) so that on Gitaly startup any interrupted `DisconnectGitAlternates` operation can be automatically detected and either completed or rolled back from the backup file without manual intervention.
- Alternatively, route `DisconnectGitAlternates` fully through the transactional WAL/FS abstraction (as is already partially done via `storage.ExtractTransaction(ctx)` in `alternates.go`) so that the rename and connectivity check are part of an atomic, crash-recoverable transaction rather than direct filesystem mutations guarded only by an in-process `defer`.
- At minimum, add a startup/health-check routine that scans for orphaned `objects/info/alternates.<timestamp>.<hex>` backup files and automatically restores them (or alerts) instead of relying solely on manual administrator action.

### Proof of Concept
1. Create a repository linked to an object pool (fork scenario) such that the repository depends on pool objects via `objects/info/alternates`.
2. Invoke `DisconnectGitAlternates` on the member repository.
3. Kill the Gitaly process (e.g., `kill -9`) at the moment right after `os.Rename(altFile, backupFile)` executes in `removeAlternatesIfOk` but before the `git rev-list` connectivity check / rollback closure completes [7](#0-6) .
4. Restart Gitaly. Observe that `objects/info/alternates` remains missing and any RPC referencing objects that live only in the pool (e.g., `CommitService.FindCommit`) fails, while only a backup file `objects/info/alternates.<ts>.<hex>` remains on disk with no automated mechanism to restore it, matching the documented "broken state until an administrator intervenes" scenario [8](#0-7) .

### Citations

**File:** internal/git/objectpool/disconnect.go (L30-35)
```go
//     the repository is reconnected to the object pool via the backup and an error returned.
//
// This operation carries some risk. If the repository is in a broken state, it will not be restored
// until after the connectivity check completes. If Gitaly crashes before the backup is restored,
// the repository may be in a broken state until an administrator intervenes and restores the backed
// up copy of objects/info/alternates.
```

**File:** internal/git/objectpool/disconnect.go (L99-117)
```go
	for _, path := range objectFiles {
		sourceRelativePath, err := filepath.Rel(f.Root(), filepath.Join(altObjectDir, path))
		if err != nil {
			return fmt.Errorf("source relative path: %w", err)
		}
		targetRelativePath := filepath.Join(repositoryRelativePath, "objects", path)

		if err := storage.MkdirAll(f, filepath.Dir(targetRelativePath)); err != nil {
			return err
		}

		if err := storage.Link(f, sourceRelativePath, targetRelativePath); err != nil {
			if errors.Is(err, fs.ErrExist) {
				continue
			}

			return err
		}
	}
```

**File:** internal/git/objectpool/disconnect.go (L203-211)
```go
// removeAlternatesIfOk is dangerous. We optimistically temporarily
// rename objects/info/alternates, and run `git fsck` to see if the
// resulting repo is connected. If this fails we restore
// objects/info/alternates. If the repo is not connected for whatever
// reason, then until this function returns, probably **all concurrent
// RPC calls to the repo will fail**. Also, if Gitaly crashes in the
// middle of this function, the repo is left in a broken state. We do
// take care to leave a copy of the alternates file, so that it can be
// manually restored by an administrator if needed.
```

**File:** internal/git/objectpool/disconnect.go (L222-265)
```go
	if err := os.Rename(altFile, backupFile); err != nil {
		return err
	}

	rollback := true
	defer func() {
		if !rollback {
			return
		}

		// If we would do a os.Rename, and then someone else comes and clobbers
		// our file, it's gone forever. This trick with os.Link and os.Rename
		// is equivalent to "cp $backupFile $altFile", meaning backupFile is
		// preserved for possible forensic use.
		tmp := backupFile + ".2"

		if err := os.Link(backupFile, tmp); err != nil {
			logger.WithError(err).ErrorContext(ctx, "copy backup alternates file")
			return
		}

		if err := os.Rename(tmp, altFile); err != nil {
			logger.WithError(err).ErrorContext(ctx, "restore backup alternates file")
		}
	}()

	// The choice here of git rev-list is for performance reasons.
	// git fsck --connectivity-only performed badly for large
	// repositories. The reasons are detailed in https://lore.kernel.org/git/9304B938-4A59-456B-B091-DBBCAA1823B2@gmail.com/
	cmd, err := repo.Exec(ctx, gitcmd.Command{
		Name: "rev-list",
		Flags: []gitcmd.Option{
			gitcmd.Flag{Name: "--objects"},
			gitcmd.Flag{Name: "--all"},
			gitcmd.Flag{Name: "--quiet"},
		},
	})
	if err != nil {
		return err
	}

	if err := cmd.Wait(); err != nil {
		return &connectivityError{error: err}
	}
```

**File:** internal/gitaly/service/objectpool/alternates.go (L13-18)
```go
// DisconnectGitAlternates is a slightly dangerous RPC. It optimistically hard-links all alternate
// objects we might need, and then temporarily removes (renames) objects/info/alternates and runs
// a connectivity check. If we are unlucky that leaves the repository in a broken state during the
// connectivity check. If we are very unlucky and Gitaly crashes, the repository stays in a broken
// state until an administrator intervenes and restores the backed-up copy of
// objects/info/alternates.
```

**File:** internal/praefect/replicator.go (L118-124)
```go
	// If the target repository is linked to a non-matching object pool it must be disconnected.
	case targetPool != nil:
		if _, err := targetObjectPoolClient.DisconnectGitAlternates(ctx, &gitalypb.DisconnectGitAlternatesRequest{
			Repository: targetRepository,
		}); err != nil {
			return err
		}
```
