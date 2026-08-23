### Title
Unbounded `committedEntries` list walk under global mutex enables write-throughput griefing of transaction commits - (File: internal/gitaly/storage/storagemgr/partition/transaction_manager.go)

### Summary
`TransactionManager.walkCommittedEntries` linearly scans the in-memory linked list `mgr.committedEntries` on every transaction commit that needs conflict verification (key-value reads, housekeeping/pack-refs, repacking). The scan is held under `mgr.mutex`, the same mutex that serializes transaction admission bookkeeping across the whole partition. An ordinary, unprivileged client that repeatedly pushes small reference updates to any repository in the partition (or to a pool and its member repositories, which share a `TransactionManager`) can keep growing this list while a long-lived reader (e.g., a slow housekeeping transaction or a long-running snapshot reader) holds back the removal of old entries, forcing every subsequent commit to pay an ever-increasing, mutex-held linear cost.

### Finding Description
`committedEntries` is a `*list.List` that keeps every committed log entry needed for conflict verification of transactions still reading an older snapshot: [1](#0-0) 

Entries are only removed from the front of the list once their `snapshotReaders` counter reaches zero, in `cleanCommittedEntry`: [2](#0-1) 

Every commit that needs to verify conflicts (key-value operations, housekeeping conflicts, `pack-refs`/repack verification) calls `walkCommittedEntries`, which iterates the entire list from `Front()` to `Back()` while holding `mgr.mutex`: [3](#0-2) 

This is directly analogous to the reported bug: a walk that continues through a growing list of unrelated entries until a matching condition (here, `committed.lsn <= transaction.snapshotLSN` continue / matching `RelativePath`) is satisfied, where the growth of "wrong direction" entries is attacker-controllable. Here, an ordinary user issuing many cheap, fast ref-update pushes (`git push` triggering `Commit`) inflates `committedEntries` for as long as any reader with an older `snapshotLSN` remains outstanding — this can be a legitimate but slow-running housekeeping transaction (repack/pack-refs, which explicitly calls `walkCommittedEntries` too, compounding the cost: `verifyHousekeeping` at [4](#0-3) , `verifyPackRefsFiles` at [5](#0-4) , `verifyRepacking` at [6](#0-5) ) or simply a client that begins a read/snapshot early and delays its subsequent write (`Begin` then `Commit` far apart in time), which pins `committedEntries.Front()`'s `snapshotReaders` above zero.

Because the walk is unconditionally performed while `mgr.mutex` is held — the same lock guarding `Begin`/snapshot bookkeeping for the whole partition — every concurrent transaction on the partition, not just the attacker's, incurs the cost, and the mutex hold also blocks other transactions from beginning or completing, amplifying the effect into a broader stall of the partition's transaction manager.

There is no visible cap on the length of `committedEntries`, nor a limit on how many transactions can be committed while a reader is outstanding, matching the "no bound on adversarial-controlled list growth traversed synchronously" bug class from the report.

### Impact Explanation
An unprivileged git user with normal push access to a repository serviced by a given partition can grow the shared `committedEntries` list arbitrarily by issuing many rapid small reference-update pushes while at least one reader (their own long-open transaction, or a concurrently running housekeeping task) keeps the list's head pinned. Every subsequent transaction commit on the partition (potentially affecting other users/repositories sharing the partition, e.g. via an object pool) must then pay an O(n) mutex-held cost in `walkCommittedEntries`, degrading commit throughput and latency for the whole partition — a DoS of the RPC-handling commit path, not limited to the attacker's own operations.

### Likelihood Explanation
Reachable purely through ordinary Git operations (`git push`) that route through `TransactionManager.Begin`/`Commit`; no privileged access, malicious peer, or leaked token is required. The attacker only needs the ability to push cheaply and to keep one reader outstanding (trivial: hold a `Begin`ed transaction open, or push during a legitimate housekeeping run, both of which are normal, reachable states). This makes the likelihood high for any partition under moderate write load combined with concurrent long-running readers/housekeeping.

### Recommendation
Bound the growth of `committedEntries` (e.g., cap outstanding entries or reader-pinned duration, forcibly failing/aborting excessively stale readers), and avoid doing the O(n) verification walk under the same global `mgr.mutex` used for transaction admission — e.g., snapshot the relevant entries under a brief lock and perform the scan (and any filtering, per `relativePath`) outside the lock, or index committed entries by `relativePath`/LSN to allow verification to skip irrelevant entries rather than a linear scan of the full list.

### Proof of Concept
Not executable from this analysis (no test harness access in this session), but the code path can be exercised as follows:
1. Client A begins a transaction (`Begin`) against a repository in partition P and does not commit immediately, pinning `committedEntries.Front().snapshotReaders > 0` (see `updateCommittedEntry` at [7](#0-6) ).
2. Client B (attacker, unprivileged) repeatedly performs small, valid `git push` operations creating many `Commit` calls with reference updates against any repository sharing the same `TransactionManager`/partition (including pool members).
3. Because Client A's read is still outstanding, `cleanCommittedEntry` cannot trim the list ( [2](#0-1) ), so `committedEntries` grows unboundedly with each of Client B's commits.
4. Any transaction needing conflict verification (key-value read-set check, housekeeping, pack-refs, repack) — from any user on the partition — now calls `walkCommittedEntries`, which is O(n) in the size of `committedEntries` and executed under `mgr.mutex`, delaying all concurrent commits on the partition.

### Citations

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager.go (L998-1005)
```go
	// appliedLSN holds the LSN of the last log entry applied to the partition.
	appliedLSN storage.LSN
	// committedEntries keeps some latest appended log entries around. Some types of transactions, such as
	// housekeeping, operate on snapshot repository. There is a gap between transaction doing its work and the time
	// when it is committed. They need to verify if concurrent operations can cause conflict. These log entries are
	// still kept around even after they are applied. They are removed when there are no active readers accessing
	// the corresponding snapshots.
	committedEntries *list.List
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

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager.go (L2318-2345)
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
```

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager_housekeeping.go (L479-508)
```go
	// Check for any concurrent housekeeping between this transaction's snapshot LSN and the latest appended LSN.
	if err := mgr.walkCommittedEntries(transaction, func(entry *gitalypb.LogEntry, objectDependencies map[git.ObjectID]struct{}) error {
		if entry.GetHousekeeping() != nil {
			return errHousekeepingConflictConcurrent
		}
		if entry.GetRepositoryDeletion() != nil {
			return errConflictRepositoryDeletion
		}

		// Applying a repacking operation prunes all loose objects on application. If loose objects were concurrently introduced
		// in the repository with the repacking operation, this could lead to corruption if we prune a loose object that is needed.
		// Transactions in general only introduce packs, not loose objects. The only exception to this currently is alternate
		// unlinking operations where the objects of the alternate are hard linked into the member repository. This can technically
		// still introduce loose objects into the repository and trigger this problem as the pools could still have loose objects
		// in them until the first repack.
		//
		// Check if the repository was unlinked from an alternate concurrently.
		for _, op := range entry.GetOperations() {
			switch op := op.GetOperation().(type) {
			case *gitalypb.LogEntry_Operation_RemoveDirectoryEntry_:
				if string(op.RemoveDirectoryEntry.GetPath()) == stats.AlternatesFilePath(transaction.relativePath) {
					return errConcurrentAlternateUnlink
				}
			}
		}

		return nil
	}); err != nil {
		return nil, fmt.Errorf("walking committed entries: %w", err)
	}
```

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager_housekeeping.go (L619-627)
```go
	if err := mgr.walkCommittedEntries(transaction, func(entry *gitalypb.LogEntry, txnObjectDependencies map[git.ObjectID]struct{}) error {
		for oid := range txnObjectDependencies {
			objectDependencies[oid] = struct{}{}
		}

		return nil
	}); err != nil {
		return fmt.Errorf("walking committed entries: %w", err)
	}
```

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager_housekeeping.go (L730-752)
```go
	// Check for any concurrent ref deletion between this transaction's snapshot LSN to the end.
	if err := mgr.walkCommittedEntries(transaction, func(entry *gitalypb.LogEntry, objectDependencies map[git.ObjectID]struct{}) error {
		for _, refTransaction := range entry.GetReferenceTransactions() {
			for _, change := range refTransaction.GetChanges() {
				// We handle HEAD updates through the git-update-ref, but since
				// it is not part of the packed-refs file, we don't need to worry about it.
				if bytes.Equal(change.GetReferenceName(), []byte("HEAD")) {
					continue
				}

				if git.ObjectID(change.GetNewOid()) == zeroOID {
					// Oops, there is a reference deletion. Bail out.
					return errPackRefsConflictRefDeletion
				}
				// Ref update. Remove the updated ref from the list of pruned refs so that the
				// new OID in loose reference shadows the outdated OID in packed-refs.
				delete(packRefs.PrunedRefs, git.ReferenceName(change.GetReferenceName()))
			}
		}
		return nil
	}); err != nil {
		return nil, fmt.Errorf("walking committed entries: %w", err)
	}
```
