### Title
Partial-failure data loss in WAL trailing-entry deletion due to non-atomic `appendedLSN` update and unconditional temp-directory cleanup - ([File: internal/gitaly/storage/storagemgr/partition/log/log_manager.go])

### Summary
`Manager.DeleteTrailingLogEntries` in `internal/gitaly/storage/storagemgr/partition/log/log_manager.go` moves WAL log-entry directories into a temporary directory one-by-one while decrementing the in-memory `appendedLSN` counter after each successful rename, but the `defer` that removes the temporary directory (`os.RemoveAll(tmpDir)`) runs unconditionally, including when the loop returns early due to a rename error. This mirrors the reported bug class: a persistent/authoritative state value (`valueHighPoint` in the original report, here `appendedLSN`) is advanced/committed based on partial progress, while the corresponding data that should back that state (the fees owed / here, the WAL log entries) is irrecoverably discarded even though the operation as a whole did not complete successfully.

### Finding Description
```go
// internal/gitaly/storage/storagemgr/partition/log/log_manager.go:424-475
func (mgr *Manager) DeleteTrailingLogEntries(from storage.LSN) (returnedErr error) {
    ...
    tmpDir, err := os.MkdirTemp(mgr.tmpDirectory, "")
    ...
    defer func() {
        if err := os.RemoveAll(tmpDir); err != nil {
            returnedErr = errors.Join(returnedErr, fmt.Errorf("remove files: %w", err))
        }
    }()

    for lsn := mgr.appendedLSN; lsn >= from; lsn-- {
        source := EntryPath(mgr.stateDirectory, lsn)
        destination := filepath.Join(tmpDir, fmt.Sprintf("%s.%s", lsn.String(), "to_delete"))
        if err := os.Rename(source, destination); err != nil {
            return fmt.Errorf("rename: %w", err)
        }
        // Lower the appendedLSN gradually to prevent partial failure.
        mgr.appendedLSN = lsn - 1
    }
    ...
}
```
The comment explicitly states the intent is "to prevent partial failure," yet the implementation does the opposite of what the fees bug's fix requires: rather than making the operation atomic (or preserving the un-renamed/undeleted entries on error so they can be retried), each already-renamed entry is moved out of `EntryPath` (removing it from the canonical WAL location) and is then unconditionally destroyed by the deferred `os.RemoveAll(tmpDir)` regardless of whether the function returns an error. If a `os.Rename` call fails partway through the loop (e.g., due to a concurrent operation, filesystem error, or an out-of-band file removal), the function returns an error, but:

1. `mgr.appendedLSN` has already been permanently lowered to reflect the entries removed so far (the "excess" work already performed is baked into persistent in-memory state, analogous to `valueHighPoint` being set to `poolValue - fees` even when only part of the fees were actually collected).
2. The WAL entries that were already renamed into `tmpDir` are deleted by the `defer`, with no path to restore them, even though the overall `DeleteTrailingLogEntries` call reports failure to its caller (raft log truncation in `internal/gitaly/storage/raftmgr/replica_log_store.go`).

This means callers that check the returned error and expect a no-op/rollback semantics on failure will instead observe a manager whose `appendedLSN` has decreased and whose on-disk WAL entries between `from` and the old `appendedLSN` are gone — data loss that is silently inconsistent with the reported error, in direct analogy to the "fees minimized/excess lost" pattern (partial progress permanently and silently consumed rather than fully rolled back or fully preserved).

### Impact Explanation
`DeleteTrailingLogEntries` is invoked by the Raft log store (`internal/gitaly/storage/raftmgr/replica_log_store.go`) to truncate the local WAL when replica log entries must be discarded and replaced (e.g., during Raft leader changes / conflicting-entry truncation, which is normal, attacker-influenceable Raft log-replication behavior). Losing WAL entries that the Raft state machine still believes are present (because the call returned an error and the caller may retry or treat the state as unchanged) can corrupt the storage partition's write-ahead log, causing inconsistency between the Raft log and the durable state on a specific node, and potential data loss or a stuck/inconsistent partition requiring manual recovery — a DoS-class and data-integrity impact on the affected storage/replica.

### Likelihood Explanation
Triggering a rename failure mid-loop requires an adverse condition (e.g., concurrent file removal, filesystem-level errors, restricted permissions on a subset of WAL directories, or filesystem corruption) during a Raft-triggered log truncation. This is not attacker-controlled through a simple crafted RPC field, but it is reachable through ordinary Raft replication/truncation logic that runs as part of normal fetch/push-driven partition operations, and any transient I/O error (which is common in production storage environments, e.g., ENOSPC, EIO, or races with pruning) during truncation is sufficient to trigger the partial-loss condition — making likelihood moderate rather than purely theoretical.

### Recommendation
Make `DeleteTrailingLogEntries` atomic with respect to both the in-memory `appendedLSN` and durable WAL state:
- Only update `mgr.appendedLSN` after all entries have been successfully staged for deletion (i.e., move the decrement out of the loop and set it once, after the loop completes without error), or roll it back explicitly if an error occurs mid-loop.
- Only invoke `os.RemoveAll(tmpDir)` (the permanent, irreversible destruction step) if the entire rename loop succeeded; on error, rename the already-moved entries back to their original `EntryPath` locations (or otherwise preserve them) before returning, so a failed call is a true no-op rather than a partial, silent, and unrecoverable deletion.

### Proof of Concept
1. Set up a `Manager` with several appended WAL log entries (LSNs `from`..`appendedLSN`).
2. Trigger `DeleteTrailingLogEntries(from)` while concurrently making the on-disk directory for one of the middle LSNs (e.g., `from+1`) inaccessible or removed out-of-band (simulating an I/O/rename failure), so `os.Rename` fails partway through the loop.
3. Observe that `DeleteTrailingLogEntries` returns a non-nil error, but `mgr.appendedLSN` has already been lowered past several LSNs and the previously renamed entries under `tmpDir` are deleted by the deferred `os.RemoveAll`, leaving those WAL entries permanently gone from disk while the call reports failure — a state inconsistent with a true "no-op on error" contract.

Note: I was not able to fully trace how `replica_log_store.go` handles the returned error from `DeleteTrailingLogEntries` (i.e., whether it retries, treats it as fatal, or assumes no state change) due to index/context limits; a full Devin session with repository access would be needed to confirm the exact downstream consequence and to validate the reproduction steps against the live raft log store call site.