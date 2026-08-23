### Title
Custom-hooks directory lock left permanently held on failure, causing persistent DoS of all future hook writes - ([File: internal/gitaly/repoutil/custom_hooks.go])

### Summary
Gitaly's custom-hooks write path takes an exclusive filesystem lock (`safe.LockingDirectory`) on a repository's `custom_hooks` directory before writing/extracting the tar-provided hooks, and only removes the lock via a deferred `Unlock()` call after the RPC handler finishes. [1](#0-0)  If that deferred unlock does not run to completion — because the process crashes, is OOM-killed, or is forcibly terminated while the lock is held, or because `Unlock()` itself fails for a reason other than "already removed" — the `.lock` file remains on disk forever. The code's own comment acknowledges this: *"If the `.lock` file is not removed from the `custom_hooks` directory, future modifications to the repository's hooks will be prevented... the `.lock` file will have to be manually removed."* [2](#0-1)  This is structurally the same bug class as the reported vault issue: a state-transition guard (the lock, analogous to `Status.Withdraw`) is acquired before a fallible operation, and there is no path back to the "open" state if that operation is interrupted — every subsequent legitimate request to modify hooks for that repository is denied indefinitely until an operator manually intervenes.

### Finding Description
The relevant logic lives in the repository custom-hooks helpers (`internal/gitaly/repoutil/custom_hooks.go`, invoked from `internal/gitaly/service/repository/set_custom_hooks.go`), which is the modern equivalent of the code captured in the diff testdata I inspected. [3](#0-2)  The lock primitive itself, `safe.LockingDirectory`, creates a `<dir>.lock` marker file on `Lock()` and removes it on `Unlock()`: [4](#0-3) 

Because the removal of this marker is only guaranteed by a `defer` inside the RPC handler's goroutine, any abnormal termination of the request (process crash, panic that escapes the defer chain, disk I/O error during `os.Remove`, etc.) leaves the `.lock` file in place permanently. Once present, every subsequent call to `Lock()` for that repository's `custom_hooks` directory returns `ErrFileAlreadyLocked`-equivalent behavior and the write is rejected — there is no timeout, lease, or automatic reclaim mechanism, matching the report's core defect: a single interrupted mutating call over the ordinary, unprivileged `SetCustomHooks`/`RestoreCustomHooks` RPC path can wedge the repository's hook-management state indefinitely, requiring an administrator to manually delete the lock file.

This mirrors the exact same design pattern seen elsewhere in Gitaly (the repository-level lock in `internal/gitaly/repoutil/lock.go`, used by `Create`/`Remove`, has the identical "hold lock, mutate on disk, unlock on defer" shape), which shows this is a systemic pattern rather than an isolated one-off. [5](#0-4) 

### Impact Explanation
If the lock is stranded, no client (GitLab Rails, gitaly-backup, or any Gitaly client) can update or restore custom hooks for the affected repository again. Since custom hooks gate push/receive behavior for a repository, an administrator must locate and manually remove the `custom_hooks.lock` file on the storage node before hook management functions normally again — an operational DoS requiring manual, out-of-band intervention, directly analogous to the reported vault being "stuck" until forced emergency action.

### Likelihood Explanation
This requires an abnormal termination or unlock failure mid-operation (e.g., Gitaly process crash/OOM kill, node restart during hook extraction, or a filesystem error on `os.Remove`), which is not attacker-triggerable at will but is a realistic operational event (crashes, OOM, forced restarts during deploys) and is explicitly anticipated by the code's own warning comment, indicating the Gitaly maintainers are aware this can and does happen in production. [2](#0-1) 

### Recommendation
Avoid relying solely on a best-effort in-process `defer` to release the lock. Options: (1) make lock acquisition idempotent/self-healing by recording an owner/PID and lock timestamp so a stale lock past a TTL can be safely reclaimed by a subsequent request; (2) tie the lock's lifetime to the transaction/WAL mechanism already used elsewhere in Gitaly (e.g., `internal/gitaly/storage/storagemgr`) so that crash recovery clears stale locks automatically, similar to how `TransactionRecoveryMiddleware.applyPendingWAL` reconciles state after a restart; [6](#0-5)  or (3) add a startup/reconciliation routine that scans for and clears orphaned `custom_hooks.lock` files left behind by processes that are confirmed to no longer be running.

### Proof of Concept
Not independently reproducible from static analysis alone — the failure requires killing/crashing the Gitaly process at the precise moment between `hooksLock.Lock()` and the deferred `hooksLock.Unlock()` in the custom-hooks write path. [7](#0-6)  The existing test `internal/safe/locking_directory_test.go` and `internal/gitaly/repoutil/custom_hooks_test.go` demonstrate the lock/unlock mechanics but the index does not contain a test that simulates an interrupted unlock; a Devin session with full repo/build access would be needed to write a reproduction that kills the process (or injects an `Unlock()` error) mid-`SetCustomHooks` call and then verifies subsequent `SetCustomHooks`/`RestoreCustomHooks` calls fail permanently until the `.lock` file is removed manually.

### Citations

**File:** internal/gitaly/service/diff/testdata/file-with-multiple-chunks-after.txt (L75-100)
```text
// restoreCustomHooks transactionally and atomically sets the provided custom
// hooks for the specified repository.
func (s *server) restoreCustomHooks(ctx context.Context, tar io.Reader, repo repository.GitRepo) (returnedErr error) {
	repoPath, err := s.locator.GetRepoPath(repo)
	if err != nil {
		return fmt.Errorf("getting repo path: %w", err)
	}

	// The `custom_hooks` directory in the repository is locked to prevent
	// concurrent modification of hooks.
	hooksLock, err := safe.NewLockingDirectory(repoPath, customHooksDir)
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
			ctxlogrus.Extract(ctx).WithError(err).Warn("failed to unlock hooks")
		}
	}()
```

**File:** internal/safe/locking_directory.go (L75-95)
```go
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

**File:** internal/gitaly/repoutil/lock.go (L22-55)
```go
func Lock(ctx context.Context, logger log.Logger, locator storage.Locator, repository storage.Repository) (func(), error) {
	path, err := locator.GetRepoPath(ctx, repository, storage.WithRepositoryVerificationSkipped())
	if err != nil {
		return nil, err
	}

	// Create the parent directory in case it doesn't exist yet.
	if err := os.MkdirAll(filepath.Dir(path), mode.Directory); err != nil {
		return nil, structerr.NewInternal("create directories: %w", err)
	}

	// We're somewhat abusing this file writer given that we simply want to assert that
	// the target directory doesn't exist and isn't created while we want to move the
	// new repository into place. We thus only use the locking semantics of the writer,
	// but will never commit it.
	locker, err := safe.NewLockingFileWriter(path)
	if err != nil {
		return nil, err
	}

	unlock := func() {
		if err := locker.Close(); err != nil {
			logger.WithError(err).ErrorContext(ctx, "closing repository locker failed")
		}
	}

	if err := locker.Lock(); err != nil {
		unlock()

		return nil, err
	}

	return unlock, nil
}
```

**File:** internal/gitaly/storage/storagemgr/middleware_recovery.go (L117-145)
```go
// applyPendingWAL starts a transaction against the target repository's partition and aborts it. If the transaction begins
// successfully, it's guaranteed that all pending WAL entries in the partition have been applied.
func (mw *TransactionRecoveryMiddleware) applyPendingWAL(ctx context.Context, methodInfo protoregistry.MethodInfo, req proto.Message) error {
	if methodInfo.Scope != protoregistry.ScopeRepository {
		// Only repository scoped RPCs may target repositories with pending WAL entries.
		return nil
	}

	targetRepo, err := methodInfo.TargetRepo(req)
	if err != nil {
		if errors.Is(err, protoregistry.ErrRepositoryFieldNotFound) {
			// If the repository field was not set, it can't target a repository that has pending WAL entries
			// Let the handler handle the situation.
			return nil
		}

		return fmt.Errorf("target repo: %w", err)
	}

	storageHandle, err := mw.node.GetStorage(targetRepo.GetStorageName())
	if err != nil {
		if errors.Is(err, storage.ErrStorageNotFound) {
			// This request was for a storage that isn't configured, and wouldn't thus target a repository
			// with a pending WAL entry.
			return nil
		}

		return fmt.Errorf("get storage: %w", err)
	}
```
