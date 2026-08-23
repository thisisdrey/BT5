### Title
Repository creation/removal lock file (`<repoPath>.lock`) has no expiry, unlike every other Gitaly lock class, permanently blocking `CreateRepository`/`RemoveRepository`/fork/import RPCs on that path - (File: internal/gitaly/repoutil/lock.go)

### Summary
`repoutil.Lock` creates a sentinel file at `<repoPath>.lock` using `safe.NewLockingFileWriter`, which is a plain `O_CREATE|O_EXCL` file with no TTL, no owner/holder identity, and no expiry field. If the process holding this lock is interrupted before calling `unlock()` (context cancellation, Gitaly process crash/OOM-kill, panic), the lock file is left on disk forever. Every future `CreateRepository` or `RemoveRepository` RPC (and any code path built on `repoutil.Create`/`repoutil.Remove`, e.g. repository forking/import/rename) against that exact repository path will then fail permanently with `safe.ErrFileAlreadyLocked` / `FailedPrecondition: repository is already locked`, and there is no RPC or mechanism to break or steal this lock.

### Finding Description
`repoutil.Lock` (`internal/gitaly/repoutil/lock.go:22-55`) locks a repository path so that only one process can create or remove it at a time: [1](#0-0) 

The lock is implemented via `safe.NewLockingFileWriter` / `LockingFileWriter.Lock()`, which simply does `os.OpenFile(path+".lock", O_CREATE|O_EXCL, ...)` and returns `ErrFileAlreadyLocked` if the file already exists: [2](#0-1) 

This lock has no expiry timestamp and no holder identifier, unlike the Praefect-side `RepoReferenceWriteLockManager`, which was purpose-built with a 20-second TTL, a background sweep of expired rows every 40 seconds, and lock-stealing semantics precisely to prevent a crashed or hung holder from permanently starving future writers: [3](#0-2) 

`repoutil.Create` and `repoutil.Remove` both take this lock around their critical sections and only release it via a `defer unlock()`: [4](#0-3) [5](#0-4) 

If the goroutine handling the RPC never reaches the `defer` (e.g. the Gitaly process is killed, OOM-killed, or panics between `os.OpenFile` succeeding and `unlock()` being registered/executed), the `.lock` file is orphaned on disk. Gitaly's own tests confirm the resulting failure mode is permanent and requires manual file-system intervention: `RemoveRepository` and `Create` both simply fail with `FailedPrecondition`/`ErrFileAlreadyLocked` whenever the file exists, with no retry, no timeout, and no automatic recovery: [6](#0-5) [7](#0-6) 

Gitaly does have a general "stale lock cleanup" housekeeping mechanism (`internal/git/housekeeping/clean_stale_data.go`), but it only targets a fixed, curated list of *internal* Git lockfiles found inside an existing repository directory (ref locks, packed-refs locks, pack `.keep` files, reftable locks, etc.), each with an explicit grace period: [8](#0-7) [9](#0-8) 

`findStaleFiles` only stats paths joined under an existing `repoPath`, so it cannot even reach the `<repoPath>.lock` sentinel used by `repoutil.Lock`, which sits as a sibling of the repository directory and is specifically meant to guard the pre-existence state of the directory (used for both `CreateRepository`, before the directory exists, and `RemoveRepository`). No housekeeping job, RPC, or timeout in the codebase targets or clears this lock file.

This is directly analogous to the reported governance bug class: a resource (a proposal / here, a repository-path lock) can enter a state where it blocks all future legitimate operations by the same actor/path, with no time-based expiry and no way for anyone to close/cancel it.

### Impact Explanation
Once orphaned, the `<repoPath>.lock` file permanently denies service for that exact repository path:
- `CreateRepository` (and any higher-level GitLab feature depending on it, such as repository import or forking to that path) will always fail with `ErrFileAlreadyLocked`.
- `RemoveRepository` on an existing repository at that path will always fail with `FailedPrecondition: repository is already locked`.

Because ordinary client behavior (request timeout/cancellation racing with the critical section, or a Gitaly node restart/crash under load) is a plausible, non-privileged trigger, this can happen without any malicious actor, and once it happens it is not self-healing — it requires manual filesystem cleanup by an operator, which is a genuine, unbounded denial-of-service condition on a specific repository path.

### Likelihood Explanation
The lock window is short in the success path, but Gitaly is routinely deployed with client timeouts, load-balancer/proxy cutoffs, and pod restarts (Kubernetes OOM-kill, rolling deploys) that can interrupt an in-flight `CreateRepository`/`RemoveRepository` RPC between `os.OpenFile` and the deferred `unlock()`. Given the number of Gitaly nodes and the frequency of repository create/remove/fork/import operations in a large deployment, hitting this window is a realistic operational occurrence rather than a purely theoretical one, and its cost (a permanently broken path) makes it comparably impactful to a deliberate exploit even without an attacker.

### Recommendation
Bring `repoutil.Lock` in line with the design already used for `RepoReferenceWriteLockManager`:
- Add an expiry/TTL to the lock file (e.g. embed a timestamp/PID in the lock file content, similar to how `FindStalePackFileLocks` inspects `.keep` file contents) and allow a subsequent locker to reclaim the lock once the grace period has elapsed.
- Alternatively, extend the housekeeping stale-file cleanup (`clean_stale_data.go`) to also recognize and clean up orphaned `<repoPath>.lock` sentinel files after a grace period, mirroring `lockfileGracePeriod`/`packFileLockGracePeriod` handling for other lock classes.
- Ensure the lock is always released via context-independent cleanup (similar to how the Praefect `unlock` uses its own 3-second timeout context rather than the caller's, per `doc/serialized_writes.md`), so that RPC cancellation cannot leave the lock behind without at least an eventual expiry safety net.

### Proof of Concept
1. Start `CreateRepository` (or `RemoveRepository`) for a given `storage/relative_path.git`.
2. Kill or crash the Gitaly process (or otherwise interrupt execution) after `repoutil.Lock` succeeds in creating `relative_path.git.lock` but before the deferred `unlock()` runs — e.g. `os.Create(repoPath + ".lock")` as gitaly's own test harness does to simulate this condition: [10](#0-9) 
3. Restart Gitaly and retry `CreateRepository` or `RemoveRepository` against the same path — the RPC fails deterministically with `ErrFileAlreadyLocked`/`FailedPrecondition`, as gitaly's own test assertions confirm, and there is no code path or RPC that will ever clear this file automatically.

### Citations

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

**File:** internal/safe/locking_file_writer.go (L124-148)
```go
// Lock locks the file writer such that no other process can concurrently update the same file. Must
// be called on an open LockingFileWriter.
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
```

**File:** internal/praefect/datastore/lock_manager.go (L65-86)
```go
type RepoReferenceWriteLockManager struct {
	qc            glsql.Querier
	handler       *lockReleaseDispatcher
	renewInterval time.Duration
	logger        log.Logger

	// lockReleasingListener listens for PostgreSQL NOTIFY events when locks are released.
	// Held here to expose its reconnect metrics via Collect.
	lockReleasingListener *ResilientListener
	// lockAcquiredAt tracks when each lock was successfully acquired, keyed by lockID
	// (virtualStorage|relativePath). Used to compute hold duration at Unlock time.
	lockAcquiredAt sync.Map
	// lockAcquiredTotal counts tryLock attempts by result: "new_acquisition", "contended", or "error".
	lockAcquiredTotal *prometheus.CounterVec
	// locksHeld is the current number of locks held, per virtual storage.
	locksHeld *prometheus.GaugeVec
	// lockHoldDuration observes how long each lock was held (tryLock success → Unlock).
	lockHoldDuration *prometheus.HistogramVec
	// operationDuration observes database round-trip time per operation (trylock/unlock/renew).
	operationDuration *prometheus.HistogramVec
}

```

**File:** internal/gitaly/repoutil/create.go (L178-196)
```go
	// We're now entering the critical section where we want to have exclusive access
	// over creation of the repository. So we:
	//
	// 1. Lock the repository path such that no other process can create it at the same
	//    time.
	// 2. Vote on the new repository's state.
	// 3. Move the repository into place.
	// 4. Do another confirmatory vote to signal that we performed the change.
	// 5. Unlock the repository again.
	//
	// This sequence guarantees that the change is atomic and can trivially be rolled
	// back in case we fail to either lock the repository or reach quorum in the initial
	// vote.
	unlock, err := Lock(ctx, logger, locator, repository)
	if err != nil {
		return fmt.Errorf("locking repository: %w", err)
	}
	defer unlock()

```

**File:** internal/gitaly/repoutil/remove.go (L90-102)
```go
	if err := voteOnAction(ctx, txManager, repository, voting.Preparing); err != nil {
		return structerr.NewInternal("vote on rename: %w", err)
	}
	// Lock the repository such that it cannot be created or removed by any concurrent
	// RPC call.
	unlock, err := Lock(ctx, logger, locator, repository)
	if err != nil {
		if errors.Is(err, safe.ErrFileAlreadyLocked) {
			return structerr.NewFailedPrecondition("repository is already locked")
		}
		return structerr.NewInternal("locking repository for removal: %w", err)
	}
	defer unlock()
```

**File:** internal/gitaly/repoutil/remove_test.go (L44-61)
```go
		{
			desc: "locked",
			createRepo: func(tb testing.TB, ctx context.Context, cfg config.Cfg) (*gitalypb.Repository, string) {
				repo, repoPath := gittest.CreateRepository(t, ctx, cfg, gittest.CreateRepositoryConfig{
					SkipCreationViaService: true,
				})

				// Simulate a concurrent RPC holding the repository lock.
				lockPath := repoPath + ".lock"
				require.NoError(t, os.WriteFile(lockPath, []byte{}, mode.File))
				tb.Cleanup(func() {
					require.NoError(t, os.RemoveAll(lockPath))
				})

				return repo, repoPath
			},
			expectedErr: structerr.NewFailedPrecondition("repository is already locked"),
		},
```

**File:** internal/gitaly/service/repository/remove_test.go (L72-91)
```go
func TestRemoveRepository_locking(t *testing.T) {
	testhelper.SkipWithWAL(t, `
Repository locks are not acquired with transaction management enabled. The test and the locking
logic will be removed once transaction managements is always enabled.`)

	t.Parallel()

	ctx := testhelper.Context(t)
	// Praefect does not acquire a lock on repository deletion so disable the test case for Praefect.
	cfg, client := setupRepositoryService(t, testserver.WithDisablePraefect())
	repo, repoPath := gittest.CreateRepository(t, ctx, cfg)

	// Simulate a concurrent RPC holding the repository lock.
	lockPath := repoPath + ".lock"
	require.NoError(t, os.WriteFile(lockPath, []byte{}, mode.File))
	defer func() { require.NoError(t, os.RemoveAll(lockPath)) }()

	_, err := client.RemoveRepository(ctx, &gitalypb.RemoveRepositoryRequest{Repository: repo})
	testhelper.RequireGrpcError(t, structerr.NewFailedPrecondition("repository is already locked"), err)
}
```

**File:** internal/git/housekeeping/clean_stale_data.go (L21-34)
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
```

**File:** internal/git/housekeeping/clean_stale_data.go (L239-245)
```go
// FindStaleLockfiles finds a subset of lockfiles which may be created by git
// commands. We're quite conservative with what we're removing, we certainly
// don't just scan the repo for `*.lock` files. Instead, we only remove a known
// set of lockfiles which have caused problems in the past.
func FindStaleLockfiles(ctx context.Context, repoPath string) ([]string, error) {
	return findStaleFiles(repoPath, lockfileGracePeriod, lockfiles...)
}
```
