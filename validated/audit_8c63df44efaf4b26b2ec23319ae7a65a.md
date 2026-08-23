### Title
Unbounded growth of `TransactionManager.committedEntries` walked in full on every write commit can DoS a partition - (File: internal/gitaly/storage/storagemgr/partition/transaction_manager.go)

### Summary
`TransactionManager` keeps an in-memory linked list, `committedEntries`, of every committed write transaction that still has an active "snapshot reader" pinning it. This list is walked from the head on *every* subsequent write transaction's commit-time conflict verification. As long as a single reader transaction keeps a snapshot open, the list can grow without bound as ordinary pushes/writes continue to arrive, and every one of those pushes must pay the cost of scanning the ever-growing list, closely mirroring the `stakerTierHistory` pattern: an unbounded array extended by everyday user activity that is iterated in its entirety on every user action.

### Finding Description
`Begin()` calls `updateCommittedEntry()` for every write transaction, which either increments `snapshotReaders` on the tail entry or appends a brand-new `committedEntry{lsn, snapshotReaders: 1}` to the `committedEntries` list: [1](#0-0) 

Entries are only removed from the front of the list once their `snapshotReaders` counter drops to zero, which happens in `cleanCommittedEntry()` when a transaction that was reading at that snapshot finishes: [2](#0-1) 

At commit time, every write transaction must verify it doesn't conflict with concurrently committed transactions by calling `walkCommittedEntries`, which does a full O(n) linear scan of the list from `Front()` to `Back()`: [3](#0-2) 

This walk is invoked from multiple commit-time verification paths, including key-value conflict checks: [4](#0-3) 

Because `cleanCommittedEntry` only removes entries from the *front* of the list, and only when the *front* entry's reader count reaches zero, a single long-lived reader whose snapshot LSN is older than all subsequently committed writes will "pin" the entire list: none of the newer entries can ever be pruned until that reader finishes, no matter how many new write transactions are committed in the meantime. Any ordinary client behavior that keeps a read-only transaction open for a long time (e.g., a large/slow read RPC, or a client that stalls mid-stream) while other pushes continue to land on the same partition/repository is sufficient to grow this list unboundedly — exactly analogous to a user's `stakerTierHistory` growing via routine activity of *other* actors.

### Impact Explanation
Since `walkCommittedEntries` is called synchronously during every write transaction's commit-time verification, and the cost of that walk grows linearly with the number of pinned entries, a partition whose `committedEntries` list has been allowed to grow large will see the CPU/latency cost of *every subsequent write* (push, ref update, housekeeping commit) increase linearly and cumulatively. In the object-pool case a partition backs many fork/pool member repositories sharing the same `TransactionManager`, so all writers targeting that partition are affected, not just the slow-reading client — a DoS of the RPC handler(s) responsible for committing transactions on that partition. Because `mgr.mutex` is held for the duration of the walk, this also serializes and blocks other `Begin()`/commit calls contending for the same lock, compounding the effect.

### Likelihood Explanation
Reachable purely by ordinary usage: any client that starts a read-only Gitaly RPC transaction against a repository/partition and is slow to consume/close it (e.g. a large blob/tree read, a slow network peer, or simply many concurrent long fetches), combined with continued writes (pushes) to the same repository or another repository in the same partition, is sufficient to grow `committedEntries` unboundedly. No privileged access, malicious peer collusion, or leaked credentials are required — only normal push/fetch traffic mixed with a slow consumer, which is easy for any unprivileged, ordinary Gitaly client to trigger, intentionally or not.

### Recommendation
- Bound the size/lifetime of `committedEntries` independently from `snapshotReaders`, e.g. by tracking per-reader minimum retained LSN so entries newer than the *actual* oldest active reader can still be pruned, rather than relying strictly on FIFO removal from the front.
- Enforce a maximum age/size for read-only snapshots (timeout long-lived readers) so that a stalled or slow reader cannot indefinitely pin the list.
- Consider switching the conflict-check data structure to one that supports efficient range queries (e.g., an ordered index keyed by LSN) instead of a full linear scan.

### Proof of Concept
1. Start a read-only transaction (`Begin(ctx, storage.BeginOptions{Write: false, RelativePaths: [...]})`) against a repository in a partition and keep it open (do not commit/rollback) — e.g., by holding open a long streaming RPC that internally uses a read transaction.
2. Concurrently issue many small write transactions (e.g., repeated single-ref pushes) against the same repository/partition.
3. Because the open reader's `snapshotLSN` predates all these commits, `cleanCommittedEntry` cannot remove any of the newly appended `committedEntry` records from the front of `committedEntries`, causing the list to grow by one entry per write.
4. Measure the latency of `verifyKeyValueOperations`/`walkCommittedEntries` on subsequent commits — it grows linearly with the number of writes performed while the reader stays open, degrading throughput for all writers on that partition until the long-lived reader finally finishes.

### Citations

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager.go (L1991-2028)
```go
// verifyKeyValueOperations checks the key-value operations of the transaction for conflicts and includes
// them in the log entry. The conflict checking ensures serializability. Transaction is considered to
// conflict if it read a key a concurrently committed transaction set or deleted. Iterated key prefixes
// are predicate locked.
func (mgr *TransactionManager) verifyKeyValueOperations(ctx context.Context, tx *Transaction) error {
	defer trace.StartRegion(ctx, "verifyKeyValueOperations").End()

	if readSet := tx.recordingReadWriter.ReadSet(); len(readSet) > 0 {
		if err := mgr.walkCommittedEntries(tx, func(entry *gitalypb.LogEntry, _ map[git.ObjectID]struct{}) error {
			for _, op := range entry.GetOperations() {
				var key []byte
				switch op := op.GetOperation().(type) {
				case *gitalypb.LogEntry_Operation_SetKey_:
					key = op.SetKey.GetKey()
				case *gitalypb.LogEntry_Operation_DeleteKey_:
					key = op.DeleteKey.GetKey()
				}

				stringKey := string(key)
				if _, ok := readSet[stringKey]; ok {
					return newConflictingKeyValueOperationError(stringKey)
				}

				for prefix := range tx.recordingReadWriter.PrefixesRead() {
					if bytes.HasPrefix(key, []byte(prefix)) {
						return newConflictingKeyValueOperationError(stringKey)
					}
				}
			}

			return nil
		}); err != nil {
			return fmt.Errorf("walking committed entries: %w", err)
		}
	}

	return nil
}
```

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

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager.go (L2318-2346)
```go
// cleanCommittedEntry reduces the snapshot readers counter of the committed entry. It also removes entries with no more
// readers at the head of the list.
func (mgr *TransactionManager) cleanCommittedEntry(entry *committedEntry) bool {
	entry.snapshotReaders--

	removedAnyEntry := false
	elm := mgr.committedEntries.Front()
	for elm != nil {
		front := elm.Value.(*committedEntry)
		if front.snapshotReaders > 0 {
			// If the first entry had still some snapshot readers, that means
			// our transaction was not the oldest reader. We can't remove any entries
			// as they'll still be needed for conflict checking the older transactions.
			return removedAnyEntry
		}

		mgr.committedEntries.Remove(elm)

		// It's safe to drop the transaction from the conflict detection history as there are no transactions
		// reading at an older snapshot. Since the changes are already in the transaction's snapshot, it would
		// already base its changes on them.
		mgr.conflictMgr.EvictLSN(mgr.ctx, front.lsn)
		mgr.fsHistory.EvictLSN(front.lsn)

		removedAnyEntry = true
		elm = mgr.committedEntries.Front()
	}
	return removedAnyEntry
}
```
