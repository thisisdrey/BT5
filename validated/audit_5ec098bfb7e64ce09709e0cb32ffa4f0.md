### Title
Unrecoverable Denial-of-Service of `SetCustomHooks`/`RestoreCustomHooks` via orphaned `custom_hooks.lock` — (File: internal/gitaly/repoutil/custom_hooks.go)

### Summary
`SetCustomHooks` (used by both the `SetCustomHooks` and `RestoreCustomHooks` RPCs, reachable during project import/fork/hook-restore flows) protects the `custom_hooks` directory with an exclusive lock file created via `O_CREATE|O_EXCL`. Like the `safeApprove` pattern in the report — which requires an allowance to be reset to zero before it can be set again — `LockingDirectory.Lock()` requires the `custom_hooks.lock` file to be absent before it can succeed. If that precondition is violated because a previous call terminated without running its cleanup, all future legitimate calls to `SetCustomHooks`/`RestoreCustomHooks` for that repository fail permanently, and — unlike comparable lock files elsewhere in Gitaly — there is no automatic staleness-based recovery mechanism for this specific lock.

### Finding Description
`SetCustomHooks` acquires the lock and defers its release: [1](#0-0) 

The lock itself is implemented as a plain `O_CREATE|O_EXCL` file with no expiry/TTL semantics — `Lock()` unconditionally fails with `ErrFileAlreadyLocked` if the `.lock` file already exists, and `Unlock()` simply removes it: [2](#0-1) 

Gitaly's housekeeping subsystem does maintain a mechanism to reclaim other stale lock files (`config.lock`, `HEAD.lock`, `objects/info/alternates.lock`, `commit-graph-chain.lock`, `multi-pack-index.lock`, reference locks, reftable locks, packed-refs locks, pack `.keep` files) using grace-period based scanners: [3](#0-2) [4](#0-3) 

`custom_hooks.lock` is conspicuously absent from every one of these stale-file finders (`lockfiles`, `FindStaleReferenceLocks`, `FindStalePackFileLocks`, `FindStaleReftableLock`, etc.), meaning there is no code path in the entire repository that will ever remove an orphaned `custom_hooks.lock`. The code's own comment acknowledges this gap explicitly: *"If the `.lock` file is not removed from the `custom_hooks` directory, future modifications to the repository's hooks will be prevented... the `.lock` file will have to be manually removed."* [5](#0-4) 

Just as `_approveToken`'s zero-allowance precondition can be desynchronized by an external call and permanently block the exchange's future approvals, the presence-of-lock-file precondition here can be desynchronized by any abnormal termination between `Lock()` succeeding and the deferred `Unlock()` running (process crash, OOM kill, or `Unlock()` itself returning a non-`ErrNotExist` error which is only logged, not retried or recovered) — and, unlike Gitaly's other lock types, there is no self-healing path.

### Impact Explanation
Once `custom_hooks.lock` is left behind for a repository, every subsequent `SetCustomHooks` and `RestoreCustomHooks` RPC call against that repository will fail with `hooksLock.Lock()` returning `ErrFileAlreadyLocked`, which propagates as an internal error. This permanently denies:
- Setting/restoring custom server hooks for that repository (used during project import, fork, and backup restore workflows).
- Any operation depending on `SetCustomHooks` in the import/replication/backup pipeline (`internal/backup/repository.go`, `internal/gitaly/service/repository/replicate.go`).

The only stated remediation is manual, out-of-band removal of the lock file by an operator, which is a genuine handler-level DoS with no built-in recovery — unlike every comparable lock file in the codebase.

### Likelihood Explanation
Reaching the vulnerable code only requires an ordinary/ordinary-privileged caller to trigger `SetCustomHooks`/`RestoreCustomHooks` (e.g. via project fork or import, which invoke these RPCs). Actually leaving the lock orphaned requires the Gitaly process to terminate abnormally (crash/OOM/panic) in the narrow window after `Lock()` succeeds and before the deferred `Unlock()` executes, or for `os.Remove` in `Unlock()` to fail for a non-missing-file reason. This is a real but comparatively lower-likelihood trigger than a purely logic-level bug, since it depends on an abnormal termination condition; however, it is a condition the code authors themselves flagged as a known risk, and Gitaly nodes are exposed to resource-exhaustion conditions (e.g., large imports/hook tarballs) that increase the chance of such terminations occurring precisely during this critical section.

### Recommendation
- Add `custom_hooks.lock` (and analogous lock artifacts) to the housekeeping stale-file finders in `internal/git/housekeeping/clean_stale_data.go`, using a grace period similar to `lockfileGracePeriod`, so that orphaned locks are automatically reclaimed.
- Alternatively/additionally, embed lock metadata (PID/timestamp) in the lock file, as is done conceptually for `.keep` files in `FindStalePackFileLocks`, so staleness can be safely detected and cleared without manual intervention.
- Ensure `Unlock()` failures are retried or surfaced with enough context that automated remediation (rather than only a warning log) can occur.

### Proof of Concept
1. An operator/attacker triggers `RestoreCustomHooks` (or `SetCustomHooks`) for a target repository (reachable via project import/fork flows using this RPC).
2. Gitaly's `SetCustomHooks` function reaches `hooksLock.Lock()` and succeeds, creating `<repo>/custom_hooks.lock`.
3. Before the deferred `hooksLock.Unlock()` runs, the Gitaly process is terminated abnormally (e.g., an OOM kill triggered by a large/malicious hooks tarball being extracted concurrently by other requests, or a node crash/restart) — leaving `custom_hooks.lock` on disk.
4. Every subsequent legitimate `SetCustomHooks`/`RestoreCustomHooks` call for that repository now fails at `hooksLock.Lock()` with `ErrFileAlreadyLocked`, and remains permanently broken because no stale-lock finder in `internal/git/housekeeping/clean_stale_data.go` ever targets `custom_hooks.lock`, requiring manual filesystem intervention to restore functionality — exactly matching the report's "future attempts... fail" DoS pattern caused by an un-resettable precondition.

### Citations

**File:** internal/gitaly/repoutil/custom_hooks.go (L140-157)
```go
	// The `custom_hooks` directory in the repository is locked to prevent
	// concurrent modification of hooks.
	hooksLock, err := safe.NewLockingDirectory(repoPath, CustomHooksDir)
	if err != nil {
		return fmt.Errorf("creating hooks lock: %w", err)
	}

	if err := hooksLock.Lock(); err != nil {
		return fmt.Errorf("locking hooks: %w", err)
	}
	defer func() {
		// If the `.lock` file is not removed from the `custom_hooks` directory,
		// future modifications to the repository's hooks will be prevented. If
		// this occurs, the `.lock` file will have to be manually removed.
		if err := hooksLock.Unlock(); err != nil {
			logger.WithError(err).ErrorContext(ctx, "failed to unlock hooks")
		}
	}()
```

**File:** internal/safe/locking_directory.go (L48-95)
```go
// Lock locks the directory and prevents a second process with a
// LockingDirectory from also locking the directory.
func (ld *LockingDirectory) Lock() error {
	if ld.state != lockingDirectoryStateUnlocked {
		return errors.New("locking directory not lockable")
	}

	lock, err := os.OpenFile(ld.lockPath(), os.O_CREATE|os.O_EXCL|os.O_RDONLY, mode.File)
	if err != nil {
		if os.IsExist(err) {
			return ErrFileAlreadyLocked
		}

		return fmt.Errorf("creating lock file: %w", err)
	}
	_ = lock.Close()

	ld.state = lockingDirectoryStateLocked

	return nil
}

// IsLocked returns whether or not the directory has been locked.
func (ld *LockingDirectory) IsLocked() bool {
	return ld.state == lockingDirectoryStateLocked
}

// Unlock unlocks the directory.
func (ld *LockingDirectory) Unlock() error {
	if ld.state != lockingDirectoryStateLocked {
		return errors.New("locking directory not locked")
	}

	if err := os.Remove(ld.lockPath()); err != nil {
		// A previous call might have returned an error
		// but still removed the file.
		if errors.Is(err, fs.ErrNotExist) {
			ld.state = lockingDirectoryStateUnlocked
			return nil
		}

		return fmt.Errorf("unlocking directory: %w", err)
	}

	ld.state = lockingDirectoryStateUnlocked

	return nil
}
```

**File:** internal/git/housekeeping/clean_stale_data.go (L21-44)
```go
const (
	emptyRefsGracePeriod                         = 24 * time.Hour
	deleteTempFilesOlderThanDuration             = 24 * time.Hour
	deleteTempObjectDirectoriesOlderThanDuration = 72 * time.Hour
	brokenRefsGracePeriod                        = 24 * time.Hour
	lockfileGracePeriod                          = 15 * time.Minute
	packedRefsLockGracePeriod                    = 1 * time.Hour
	packedRefsNewGracePeriod                     = 15 * time.Minute
	packFileLockGracePeriod                      = 7 * 24 * time.Hour
	// ReferenceLockfileGracePeriod is the grace period when cleaning up lock files of individual references in the
	// repository. Lock files existing less than this period are ignored.
	ReferenceLockfileGracePeriod = 1 * time.Hour
	// ReftableLockfileGracePeriod is the grace period when cleaning up lock files reftable.
	ReftableLockfileGracePeriod = 1 * time.Hour
)

var lockfiles = []string{
	"config.lock",
	"HEAD.lock",
	"info/attributes.lock",
	"objects/info/alternates.lock",
	"objects/info/commit-graphs/commit-graph-chain.lock",
	"objects/pack/multi-pack-index.lock",
}
```

**File:** internal/git/housekeeping/clean_stale_data.go (L75-104)
```go
// DefaultStaleDataCleanup is the default configuration for CleanStaleData
// which contains all the cleanup functions.
func DefaultStaleDataCleanup() CleanStaleDataConfig {
	return CleanStaleDataConfig{
		StaleFileFinders: map[string]FindStaleFileFunc{
			"objects":        FindTemporaryObjects,
			"objectdirs":     FindTemporaryObjectDirectories,
			"locks":          FindStaleLockfiles,
			"refs":           FindBrokenLooseReferences,
			"reflocks":       FindStaleReferenceLocks(ReferenceLockfileGracePeriod),
			"reftablelocks":  FindStaleReftableLock,
			"packfilelocks":  FindStalePackFileLocks,
			"packedrefslock": FindPackedRefsLock,
			"packedrefsnew":  FindPackedRefsNew,
			"serverinfo":     FindServerInfo,
		},
		RepoCleanups: map[string]CleanupRepoFunc{
			"refsemptydir":   RemoveRefEmptyDirs,
			"configsections": PruneEmptyConfigSections,
			"infoattributes": RemoveInfoAttributes,
		},
		RepoCleanupWithTxManagers: map[string]cleanupRepoWithTxManagerFunc{
			"configkeys": removeUnnecessaryConfig,
			// Gitaly used to set `gitlab.fullpath` via the SetFullPath RPC.
			// That RPC was removed in https://gitlab.com/groups/gitlab-org/-/epics/8953. This housekeeping
			// task removes the now unused repository config entry.
			"configfullpath": removeGitLabFullPathConfig,
		},
	}
}
```
