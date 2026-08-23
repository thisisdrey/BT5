### Title
Unbounded conflict-check walk over committed transaction log entries blocks the partition mutex - (File: internal/gitaly/storage/storagemgr/partition/transaction_manager.go)

### Summary
`TransactionManager.walkCommittedEntries` iterates, under the partition-wide `mgr.mutex` lock, over every entry in the in-memory `committedEntries` linked list that was committed after a transaction's snapshot LSN. The list only shrinks when the oldest entry's `snapshotReaders` counter drops to zero, so as long as any transaction is holding an old snapshot (e.g. it is slow to prepare/verify, or many concurrent writers keep taking new snapshots faster than older ones finish), the list keeps growing unbounded with every subsequent commit to the partition. Every new transaction that must reconcile against the log (conflict verification / housekeeping) then has to walk this ever-growing list while holding the shared mutex, exactly mirroring the `AI Arena` bug class where a loop bounded by "distance since last processed checkpoint" grows without limit and is executed on every user action.

### Finding Description
`walkCommittedEntries` is defined as: [1](#0-0) 

It is invoked while holding `mgr.mutex` for the whole duration of the walk. The entries are added to the list via `updateCommittedEntry`, which appends a new `committedEntry` for every committed transaction and increments a `snapshotReaders` counter for entries still referenced by in-flight transactions: [2](#0-1) 

The list is only trimmed from the front when the reader counter for the oldest entries reaches zero (`cleanCommittedEntry`), i.e., only after every transaction that began its snapshot at or before that LSN has finished. As long as ordinary write (mutator) RPCs keep committing changes to the partition faster than a slow/older transaction can finish, `committedEntries` keeps growing without any cap, exactly analogous to the round counter in `MergingPool.claimRewards`/`RankedBattle.claimNRN` that grows unbounded with every new round.

Because the walk (`walkCommittedEntries`) is performed under the same lock (`mgr.mutex`) used by the transaction commit path (see `mgr.mutex.Lock()`/`Unlock()` pattern reused elsewhere in the file, e.g. around applied-LSN notification at lines 2217–2220), a long walk directly serializes and delays every other transaction trying to commit against the same partition — including unrelated repositories that share the partition through object pools/forks. This RPC path is reachable by ordinary git push traffic; no privileged access is required to trigger many rapid commits or to hold open a slow transaction (e.g. via a very large push whose verification/preparation takes a long time) while other pushes continue to commit.

### Impact Explanation
If a single transaction's preparation/verification phase is slow (e.g. a very large push, or many small pushes overlapping with one held-open snapshot), every subsequent commit to that partition must walk a growing, unbounded list of pending committed entries while holding the shared partition mutex. This directly serializes and slows down all mutator RPCs (fetch, push, replication) against every repository sharing that partition (a repository and its pool members), producing a resource-exhaustion / DoS condition analogous to the reported hot loop, without requiring any privileged actor — only ordinary concurrent pushes.

### Likelihood Explanation
Likelihood is moderate: it requires sustained concurrent write throughput against a single partition while at least one transaction holds an older snapshot open longer than usual (e.g. due to a large object set or slow client). Both conditions are plausible in a busy repository (many small pushes racing a large one, or replication/read transactions holding snapshots). No malicious or privileged access is needed — ordinary push/fetch traffic suffices to create sustained growth of `committedEntries`.

### Recommendation
- Bound the number of unacknowledged/unread committed entries retained in memory (e.g., cap queue depth, or force old snapshot-holding transactions to fail/retry once a threshold is exceeded) instead of allowing unbounded growth tied purely to "slowest active reader."
- Avoid holding the partition-wide mutex for the entire duration of `walkCommittedEntries`; snapshot the relevant sub-range of entries under the lock and process/callback outside of it, or use a lock-free/read-copy-update structure for the log-entry list.
- Add metrics/backpressure (similar to the existing `PackObjectsLimiting`/load-shedding mechanisms already present in Gitaly) so that a partition can reject or delay new transactions once the committed-entries backlog grows beyond a safe bound, rather than silently degrading commit latency for all repositories sharing the partition.

### Proof of Concept
Not executable from the indexed context (this requires a live Gitaly cluster with instrumented transaction manager). A conceptual reproduction:
1. Start a transaction `T0` against a partition and stall it (e.g. via a debugger hook, or a very large object set) so it retains an old `snapshotLSN` and its `committedEntry.snapshotReaders` stays > 0.
2. Concurrently issue many rapid, small pushes (mutator RPCs) against the same partition (same repository or a pool member), each of which calls `updateCommittedEntry`/commits, appending to `committedEntries`.
3. Once `T0` finally verifies/commits, `walkCommittedEntries` (and any other transaction attempting to commit meanwhile) must traverse the entire accumulated list while holding `mgr.mutex`, causing measurable latency spikes/blocking for all concurrent commits on the partition — proportional to the number of entries accumulated during `T0`'s lifetime.

This could not be fully validated end-to-end against a running cluster from the static index; the exact magnitude of the resulting stall (versus other safeguards such as commit ordering/backoff not visible in the retrieved snippets) is uncertain and would need to be confirmed with a live benchmark/test in the repository (`internal/gitaly/storage/storagemgr/partition/transaction_manager_test.go` or similar).

### Citations

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager.go (L2269-2288)
```go
// updateCommittedEntry updates the reader counter of the committed entry of the snapshot that this transaction depends on.
func (mgr *TransactionManager) updateCommittedEntry(snapshotLSN storage.LSN) *committedEntry {
	// Since the goroutine doing this is holding the lock, the snapshotLSN shouldn't change and no new transactions
	// can be committed or added. That should guarantee .Back() is always the latest transaction and the one we're
	// using to base our snapshot on.
	if elm := mgr.committedEntries.Back(); elm != nil {
		entry := elm.Value.(*committedEntry)
		entry.snapshotReaders++
		return entry
	}

	entry := &committedEntry{
		lsn:             snapshotLSN,
		snapshotReaders: 1,
	}

	mgr.committedEntries.PushBack(entry)

	return entry
}
```

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager.go (L2290-2316)
```go
// walkCommittedEntries walks all committed entries after input transaction's snapshot LSN. It loads the content of the
// entry from disk and triggers the callback with entry content.
func (mgr *TransactionManager) walkCommittedEntries(transaction *Transaction, callback func(*gitalypb.LogEntry, map[git.ObjectID]struct{}) error) error {
	mgr.mutex.Lock()
	defer mgr.mutex.Unlock()

	for elm := mgr.committedEntries.Front(); elm != nil; elm = elm.Next() {
		committed := elm.Value.(*committedEntry)
		if committed.lsn <= transaction.snapshotLSN {
			continue
		}

		if committed.entry == nil {
			return errCommittedEntryGone
		}
		// Transaction manager works on the partition level, including a repository and all of its pool
		// member repositories (if any). We need to filter log entries of the repository this
		// transaction targets.
		if committed.entry.GetRelativePath() != transaction.relativePath {
			continue
		}
		if err := callback(committed.entry, committed.objectDependencies); err != nil {
			return fmt.Errorf("callback: %w", err)
		}
	}
	return nil
}
```
