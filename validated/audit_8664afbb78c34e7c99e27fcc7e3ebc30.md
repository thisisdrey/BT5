Confirmed: `TransactionManager` is the single writer per partition, processing transactions strictly sequentially via `processTransaction`/`commit` in `run()`, and `commit()` synchronously calls `prepareHousekeeping` (which includes `preparePackRefsFiles` for the `pack-refs` housekeeping task) before a transaction can be appended to the WAL and released. [1](#0-0) [2](#0-1) 

### Title
Unbounded double directory-walk of `refs/` in `preparePackRefsFiles` lets an attacker DoS a repository's single-writer transaction pipeline via cheap loose-ref spam - (File: internal/gitaly/storage/storagemgr/partition/transaction_manager_housekeeping.go)

### Summary
`preparePackRefsFiles`, invoked synchronously by the partition's single-writer `TransactionManager` whenever a `pack-refs` housekeeping task runs, performs two full `filepath.WalkDir` passes over the repository's `refs/` directory tree to detect pruned references. The cost of this operation scales linearly with however many loose references currently exist in the repository. An ordinary user with push access can cheaply inflate the number of loose references (e.g. pushing many small `UpdateReferences`/`git push` batches that each create a new lightweight branch pointing at an already-existing object, at effectively zero object-storage cost) and thereby force this walk to become extremely large. Because the `TransactionManager` is the sole, sequential writer for its partition, any transaction that triggers `pack-refs` (which the heuristic in `ShouldRepackReferences` triggers routinely once there are only ~16+ loose refs relative to a small packed-refs file) will block the entire per-repository write pipeline for the duration of the double walk, delaying or effectively denying service to all other legitimate writers of that repository/partition until the walk completes.

### Finding Description
`ShouldRepackReferences` in `internal/git/housekeeping/optimization_strategy.go` decides that references should be repacked using a heuristic that requires very few loose refs relative to the size of `packed-refs` (as low as 16 refs). [3](#0-2) 

When that heuristic fires, `TransactionManager.commit()` runs `prepareHousekeeping`, which for the `files` reference backend calls `preparePackRefsFiles`. This function first walks the entire `refs/` directory tree to build the pre-repack set of loose references, then invokes `git-pack-refs`, then walks the tree a second time to compute which references disappeared (were successfully packed) versus which remain: [4](#0-3) 

Both walks are `filepath.WalkDir` calls with no size limit, no timeout, and no cap on the number of loose refs processed — the cost is strictly O(number of refs on disk). This function is invoked from inside `commit()`, which itself runs synchronously inside the partition's single writer loop (`run()` → `processTransaction()` → the anonymous commit closure), meaning no other transaction against that repository/partition can be admitted, verified, or applied until the pack-refs preparation (including both walks and the `git-pack-refs` subprocess) finishes. [2](#0-1) [5](#0-4) 

This is structurally analogous to the reported bug class: an attacker cheaply grows an unbounded, attacker-influenced collection (loose refs, analogous to `fixedOngoingWithdrawalUsers`) through many small, low-cost operations (dust ref-creating pushes, analogous to dust deposits), and a later required "finalize"-style operation (`pack-refs` preparation, gating commit of the WAL entry, analogous to `finalizeVaultEndedWithdrawals()`) must iterate the entire unbounded collection before the system can make forward progress, causing denial of service to legitimate operations on the same repository/partition.

### Impact Explanation
Because `TransactionManager` serializes all writes to a partition (and object-pool-linked repositories/forks share partitions), an attacker who can push to a single repository can inflate the pack-refs walk cost and stall every subsequent write transaction against that repository (and any forks/pool members sharing the partition) for as long as the walk and the `git-pack-refs` subprocess take. This is a repository/partition-scoped denial of service reachable from an ordinary authenticated push, without requiring any special privilege.

### Likelihood Explanation
The `ShouldRepackReferences` heuristic triggers pack-refs very readily (with a packed-refs file under 1KB, only ~16 loose refs are needed to trigger it), so an attacker only needs to accumulate a large number of loose references before this threshold check fires. Creating loose references is cheap: `git-update-ref`/`UpdateReferences` allows creating a new ref pointing at any already-reachable object with no new object data required, so the attack cost scales only with the number of RPC calls, not with storage or object cost, matching the "spamming dust deposits" pattern of the source report.

### Recommendation
- Bound the cost of `preparePackRefsFiles` independent of how many loose refs currently exist, e.g. by capping the maximum number of loose references processed per pack-refs pass, or by having `git-pack-refs` report pruned refs directly (as already tracked as a known follow-up in the code comments) instead of requiring two full tree walks.
- Add a hard ceiling to `ShouldRepackReferences`/`LooseObjectLimit`-style heuristics so pathologically large loose-ref counts trigger a bounded/incremental repack strategy rather than one unbounded synchronous pass inside the single-writer critical section.
- Consider moving the expensive walk out of the critical path that blocks the partition's single writer, or performing it against a snapshot in a way that does not delay competing transactions' admission.

### Proof of Concept
1. As an ordinary user with push access to a repository, repeatedly call `UpdateReferences` (or perform many small `git push` operations) to create thousands of new lightweight branches, each pointing at the existing `HEAD` commit (no new objects required per push), e.g. `refs/heads/spam-0001` … `refs/heads/spam-20000`.
2. Once the loose reference count exceeds the `ShouldRepackReferences` threshold relative to the (small) `packed-refs` file size, the next write transaction against the repository triggers `prepareRepacking`/`preparePackRefsFiles` in `TransactionManager.commit()`.
3. `preparePackRefsFiles` performs two `filepath.WalkDir` passes over `refs/heads/spam-*` (tens of thousands of entries) plus a `git-pack-refs --all` invocation, all executed synchronously inside the partition's sole writer goroutine.
4. Concurrently issue an unrelated, legitimate write RPC (e.g. a normal branch update) against the same repository/partition and observe it blocked/queued in `admissionQueue`/`processTransaction` until the pack-refs preparation completes, demonstrating denial of service to other writers scoped to that repository/partition.

### Citations

**File:** doc/transactions.md (L76-78)
```markdown
A partition is started by `StorageManager` by [launching a goroutine](https://gitlab.com/gitlab-org/gitaly/-/blob/7c0f925b3df33c77de8c124b5f89447a13da3059/internal/gitaly/storage/storagemgr/partition_manager.go#L555) running [`TransactionManager.Run()`](https://gitlab.com/gitlab-org/gitaly/-/blob/7c0f925b3df33c77de8c124b5f89447a13da3059/internal/gitaly/storage/storagemgr/partition/transaction_manager.go#L1670).
This goroutine is the partition's single writer, and is the only one operating on the actual state of the partition. This goroutine runs a loop to [apply the write-ahead log](https://gitlab.com/gitlab-org/gitaly/-/blob/7c0f925b3df33c77de8c124b5f89447a13da3059/internal/gitaly/storage/storagemgr/partition/transaction_manager.go#L1705) and [process transactions](https://gitlab.com/gitlab-org/gitaly/-/blob/7c0f925b3df33c77de8c124b5f89447a13da3059/internal/gitaly/storage/storagemgr/partition/transaction_manager.go#L1711)
that are ready to commit. It also synchronizes access to the partition's state between applying the log, and creating transaction snapshots.
```

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager.go (L1114-1121)
```go
	if transaction.repositoryCreation == nil {
		if err := mgr.packObjects(ctx, transaction); err != nil {
			return 0, fmt.Errorf("pack objects: %w", err)
		}

		if err := mgr.prepareHousekeeping(ctx, transaction); err != nil {
			return 0, fmt.Errorf("preparing housekeeping: %w", err)
		}
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

**File:** internal/git/housekeeping/optimization_strategy.go (L348-371)
```go
	// Packing loose references into the packed-refs file scales with the number of references
	// we're about to write. We thus decide whether we repack refs by weighing the current size
	// of the packed-refs file against the number of loose references. This is done such that we
	// do not repack too often on repositories with a huge number of references, where we can
	// expect a lot of churn in the number of references.
	//
	// As a heuristic, we repack if the number of loose references in the repository exceeds
	// `log(packed_refs_size_in_bytes/100)/log(1.15)`, which scales as following (number of refs
	// is estimated with 100 bytes per reference):
	//
	// - 1kB ~ 10 packed refs: 16 refs
	// - 10kB ~ 100 packed refs: 33 refs
	// - 100kB ~ 1k packed refs: 49 refs
	// - 1MB ~ 10k packed refs: 66 refs
	// - 10MB ~ 100k packed refs: 82 refs
	// - 100MB ~ 1m packed refs: 99 refs
	//
	// We thus allow roughly 16 additional loose refs per factor of ten of packed refs.
	//
	// This heuristic may likely need tweaking in the future, but should serve as a good first
	// iteration.
	if uint64(math.Max(16, math.Log(float64(s.info.References.PackedReferencesSize)/100)/math.Log(1.15))) > s.info.References.LooseReferencesCount {
		return false
	}
```

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager_housekeeping.go (L1582-1660)
```go

```
