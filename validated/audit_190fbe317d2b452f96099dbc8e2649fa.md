## Title
Unvalidated quarantine objects committed to the write-ahead log can permanently halt an entire partition's transaction processing - ([File: internal/gitaly/storage/storagemgr/partition/transaction_manager.go])

### Summary
`TransactionManager.packObjects` packs objects out of a push's quarantine directory into the write-ahead log (WAL) without validating their integrity, and the code explicitly acknowledges this gap. If a maliciously or corruptly crafted object gets past this stage and is later logged/committed, `applyLogEntry` (invoked from the manager's single-writer `run()` loop) can fail when applying that log entry to disk. Unlike the audited Deposit-signature bug, where a bad item merely causes the *current* proposer to error, here a bad item that reaches the WAL is *permanent state*: every restart of the `TransactionManager` replays the log from the last applied LSN, hits the same broken entry, and fails again — halting the partition (and every repository assigned to it, including all repositories that share an object pool) until manual admin intervention, exactly matching the "each next proposer will be unable to propose a block" chain-of-events pattern in the report.

### Finding Description
`packObjects` walks new reference tips and objects present in the client's quarantine directory and bundles them into a packfile that becomes part of the transaction's WAL entry: [1](#0-0) 

The comment on this function is explicit about the gap: *"The packed objects are not yet checked for validity."* This is the state-transition analog of the Deposit case: the signature/validity check that should gate an item before it is durably accepted is deferred or skipped.

The transaction is subsequently appended to the WAL: [2](#0-1) 

and the manager's single writer goroutine applies committed log entries in order, in a tight loop that has no way to skip a bad entry: [3](#0-2) 

If `applyLogEntry` returns an error (e.g., `applyOperations` fails because the file system state it expects — built from an improperly validated pack/object — is not as expected), `run()` returns and the entire `TransactionManager` for that partition exits: [4](#0-3) 

Because the log entry was already durably appended (step "committed to WAL" happens before apply), on restart the manager will try to re-apply the *same* broken entry from LSN N again and fail identically — there is no skip/ignore path. Gitaly's own recovery tooling documents this exact irrecoverable failure mode: [5](#0-4) 

All transactions on the affected partition subsequently fail with `storage.ErrTransactionProcessingStopped`: [6](#0-5) 

Partitions are shared by an object pool and all repositories connected (or about to be connected) to it, meaning one malicious push into a fork or pool member can take down transaction processing for every repository sharing that partition: [7](#0-6) 

### Impact Explanation
This is a Denial of Service on Gitaly's write path. Once a crafted push causes an unvalidated object to be packed and logged, the partition's `TransactionManager` can crash-loop on every restart while attempting to reapply that entry, blocking all writes (and per the docs, potentially reads too, since `StorageManager` needs to restart the partition) for every repository assigned to that partition — including unrelated repositories that merely share an object pool with the attacker's fork. Recovery requires manual, out-of-band operator intervention (deleting/patching the malformed log entry), matching the report's "high severity, not critical because it can be resolved with a client update without revising chain history" characterization, translated to "requires manual ops intervention without necessarily losing repository data."

### Likelihood Explanation
Reaching `packObjects` only requires an ordinary authenticated push (or any RPC that writes through a quarantine directory, e.g. `PostReceivePack`, `UserCommitFiles`, `FetchRemote`) that a normal user can trigger. The explicit code comment and referenced tracking issue (gitlab-org/gitaly#5779) confirm the packing path is a known, currently-unvalidated gap. Getting an invalid/pathological object all the way through to a hard failure in `applyOperations` may require crafting specific object shapes; this makes exploitation non-trivial but the underlying architectural weakness (durable commit before integrity validation, non-skippable apply loop) is confirmed by the code and by Gitaly's own recovery-tool commentary describing this exact "partition always in a broken state" scenario.

### Recommendation
- Validate object/pack integrity (e.g., via `git-index-pack --strict` or an explicit connectivity/fsck-style check) before objects are admitted into the WAL entry in `packObjects`, closing gitaly-org/gitaly#5779, rather than deferring validation to apply-time.
- Ensure `applyLogEntry` failures arising from data-dependent issues are distinguished from infrastructure failures, and where the failure is due to malformed transaction content, provide a supported operator/automatic path to skip or quarantine the single offending log entry instead of the manager going into `TransactionProcessingStopped` and requiring a manual `recovery replay`-style intervention. This mirrors the recommended "skip the invalid item, don't halt everyone" fix in the report.

### Proof of Concept
Not independently reproduced; based on static analysis of `packObjects` (`internal/gitaly/storage/storagemgr/partition/transaction_manager.go:1306-1319`), the WAL append/apply loop (`transaction_manager.go:1727-1739`, `2171-2212`), and Gitaly's own documented irrecoverable failure mode in `internal/cli/gitaly/subcmd_recovery_replay.go:151-166` and the "fail to apply a log entry" test case in `internal/cli/gitaly/subcmd_recovery_test.go:559-585`, which demonstrates that a malformed committed log entry causes `transaction processing stopped` and requires manual replay tooling.

### Citations

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager.go (L1306-1319)
```go
// packObjects walks the objects in the quarantine directory and the new reference tips. All objects in
// the quarantine directory that are encountered during the walk are included in a packfile that gets
// committed with the transaction. All encountered objects that are missing from the quarantine directory
// are considered the transaction's dependencies. The dependencies are later verified to exist in the
// repository before committing the transaction, and they will be guarded against concurrent pruning
// operations. The final pack is staged in the WAL directory of the transaction ready for committing.
// The pack's index and reverse index is also included.
//
// Objects that already exist in the repository are included in the packfile if the client wrote them
// into the quarantine directory.
//
// The packed objects are not yet checked for validity. See the following issue for more
// details on this: https://gitlab.com/gitlab-org/gitaly/-/issues/5779
func (mgr *TransactionManager) packObjects(ctx context.Context, transaction *Transaction) (returnedErr error) {
```

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager.go (L1691-1695)
```go
// once they've been applied to the repository.
//
// Run keeps running until Stop is called or it encounters a fatal error. All transactions will error with
// storage.ErrTransactionProcessingStopped when Run returns.
func (mgr *TransactionManager) Run() error {
```

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager.go (L1727-1739)
```go
	for {
		if mgr.appliedLSN < mgr.logManager.AppendedLSN() {
			lsn := mgr.appliedLSN + 1
			if err := mgr.applyLogEntry(ctx, lsn); err != nil {
				return fmt.Errorf("apply log entry: %w", err)
			}
			continue
		}

		if err := mgr.processTransaction(ctx); err != nil {
			return fmt.Errorf("process transaction: %w", err)
		}
	}
```

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager.go (L2148-2169)
```go
// appendLogEntry appends a log entry of a transaction to the write-ahead log. After the log entry is appended to WAL,
// the corresponding snapshot lock and in-memory reference for the latest appended LSN is created.
func (mgr *TransactionManager) appendLogEntry(ctx context.Context, objectDependencies map[git.ObjectID]struct{}, logEntry *gitalypb.LogEntry, logEntryPath string) error {
	defer trace.StartRegion(ctx, "appendLogEntry").End()

	// After this latch block, the transaction is committed and all subsequent transactions
	// are guaranteed to read it.
	appendedLSN, err := mgr.logManager.AppendLogEntry(logEntryPath)
	if err != nil {
		return fmt.Errorf("append log entry: %w", err)
	}

	mgr.mutex.Lock()
	mgr.committedEntries.PushBack(&committedEntry{
		lsn:                appendedLSN,
		entry:              logEntry,
		objectDependencies: objectDependencies,
	})
	mgr.mutex.Unlock()

	return nil
}
```

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager.go (L2171-2212)
```go
// applyLogEntry reads a log entry at the given LSN and applies it to the repository.
func (mgr *TransactionManager) applyLogEntry(ctx context.Context, lsn storage.LSN) error {
	defer trace.StartRegion(ctx, "applyLogEntry").End()

	defer prometheus.NewTimer(mgr.metrics.transactionApplicationDurationSeconds).ObserveDuration()

	manifest, err := wal.ReadManifest(mgr.logManager.GetEntryPath(lsn))
	if err != nil {
		return fmt.Errorf("read log entry: %w", err)
	}

	// Ensure all snapshotters have finished snapshotting the previous state before we apply
	// the new state to the repository. No new snapshotters can arrive at this point. All
	// new transactions would be waiting for the committed log entry we are about to apply.
	previousLSN := lsn - 1

	mgr.mutex.Lock()
	previousLock := mgr.snapshotLocks[previousLSN]
	mgr.mutex.Unlock()

	// This might take a while, it should better wait out side of mutex lock.
	previousLock.activeSnapshotters.Wait()

	mgr.mutex.Lock()
	delete(mgr.snapshotLocks, previousLSN)
	mgr.mutex.Unlock()

	mgr.testHooks.beforeApplyLogEntry(lsn)

	if err := mgr.db.Update(func(tx keyvalue.ReadWriter) error {
		if err := applyOperations(ctx, safe.NewSyncer().Sync, mgr.storagePath, mgr.logManager.GetEntryPath(lsn), manifest.GetOperations(), tx); err != nil {
			return fmt.Errorf("apply operations: %w", err)
		}

		return nil
	}); err != nil {
		return fmt.Errorf("update: %w", err)
	}

	if err := mgr.storeAppliedLSN(lsn); err != nil {
		return fmt.Errorf("set applied LSN: %w", err)
	}
```

**File:** internal/cli/gitaly/subcmd_recovery_replay.go (L151-166)
```go
		// Wait for the log entry to be applied and verify the result
		txn, err = ptn.Begin(ctx, storage.BeginOptions{
			Write:         false,
			RelativePaths: []string{},
		})
		if err != nil || txn.SnapshotLSN() != nextLSN {
			// If a log entry cannot be applied for any reason (broken, wrong bucket, etc.), the user will
			// find out, but it requires an in-depth investigation. Until the reason is exposed, that
			// partition is always in a broken state. There is nothing this tool can do to resolve the
			// situation automatically. It's up to the user to decide the next course of actions. At latest,
			// the malformed log entry is removed. Otherwise, the partition is broken completely.
			return errors.Join(
				fmt.Errorf("failed to apply latest log entry: %w", err),
				ptn.GetLogWriter().DeleteLogEntry(nextLSN),
			)
		}
```

**File:** doc/transactions.md (L56-60)
```markdown
Gitaly automatically assigns repositories to partitions when they are first accessed:

- Object pools and all repositories connected to the object pool are placed in the same partition. Repositories that are about to be connected to an object pool, such as newly created forks, are also placed in the same partition with the object pool they are about to be connected.
  - Assigning pools and their connected repositories into the same partition ensures transactions can guarantee consistency between them. If pools were in different partitions, transaction ordering could cause issues, for example updating a reference in a fork before the objects are written into the pool.
- Repositories that are not connected (nor about to be connected) to an object pool are placed in their own partitions.
```
