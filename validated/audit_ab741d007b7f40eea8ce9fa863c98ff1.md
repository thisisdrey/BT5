### Title
Praefect crash-recovery unconditionally re-applies `IncrementGeneration`, allowing a repository's generation ledger to be advanced without a corresponding replicated write - ([File: internal/praefect/recovery.go])

### Summary
`recoverPendingTransaction()` in `internal/praefect/recovery.go` performs the Praefect analog of `PrePOMarket.setFinalLongPayout()`: it unconditionally "finalizes" a repository's generation counter (`rs.IncrementGeneration`) for a pending crash-recovery record with no check that the increment has not already been applied. Just like `finalLongPayout` could be re-set after payouts already happened, the generation ledger can be advanced a second time for the same underlying mutator, producing a mismatch between the recorded "confirmed generation" and the actual replicated state on disk, which is precisely the accounting-ledger/insolvency-style bug class in the referenced report.

### Finding Description
Every mutator RPC (e.g. an ordinary `git push` routed through `PostReceivePack`) causes the coordinator to write a `pending_transactions` crash-recovery record *before* telling Gitaly nodes to commit: [1](#0-0) 

The record is only deleted once the transaction finalizer has successfully called `IncrementGeneration`: [2](#0-1) 

If Praefect crashes/restarts between the finalizer's `IncrementGeneration` call succeeding and `DeletePendingTransaction` succeeding — an entirely ordinary operational event (OOM kill, deploy restart, node failure) that requires no attacker action — the pending record survives. On the next startup, `RecoverPendingTransactions` reprocesses every leftover record and `recoverPendingTransaction` calls `IncrementGeneration` again **unconditionally**, with the reasoning spelled out directly in the code: [3](#0-2) 

The accompanying test explicitly documents and asserts this "double-apply" behavior as intentional: [4](#0-3) 

This is structurally identical to the `setFinalLongPayout()` flaw: a state-finalizing operation (`finalLongPayout` / `generation`) that is supposed to be applied exactly once per real mutation is instead re-appliable, and the code relies on an informal invariant ("consistent nodes advance together") rather than an explicit guard (e.g. tracking whether this specific `PendingTransaction.ID` already resulted in an increment, or storing the pre-increment generation and doing a compare-and-swap).

### Impact Explanation
`storage_repositories.generation` is Praefect's sole source of truth for whether a physical replica is up to date (see `doc/virtual_storage.md`, "Generation Counters"). If the ledger is advanced for a repository without every "updated" replica actually having received the corresponding write (e.g. because a second, unrelated crash/restart cycle causes recovery to run again on a stale record, or two pending records for the same repository race against a concurrently-running new mutator's own finalizer), a replica can be marked consistent with the primary at a generation number it never actually reached content-wise. Read traffic routed to that "confirmed" but factually stale/incorrect replica then silently returns wrong data, and downstream reconciliation (which relies on generation mismatches to schedule replication) will not detect or repair the divergence because the numbers already match. This is a genuine storage-consistency corruption bug, not merely a cosmetic one, since Praefect's entire consistency/routing model is generation-number based.

### Likelihood Explanation
No malicious or privileged actor is required. The trigger conditions are: (1) any ordinary write RPC that goes through the transactional mutator path (`PostReceivePack`, `UserMergeBranch`, etc.), and (2) a Praefect crash/restart occurring in the narrow window between the finalizer's DB commit of the generation increment and the deletion of the `pending_transactions` row — a window that widens under load, during rolling restarts, or when the recovery goroutine itself is interrupted mid-loop (`RecoverPendingTransactions` logs and continues on a per-record failure, deliberately leaving records for retry, which increases the chance of the same record being reprocessed on a subsequent startup after a partial success).

### Recommendation
Make the recovery-time generation increment idempotent per pending-transaction record instead of unconditionally re-applying it:
- Persist (in the `pending_transactions` row) whether the generation increment for this record has already been performed, or record the pre-transaction generation value and only increment if the current generation still equals that baseline (a compare-and-set), mirroring the recommended fix of guarding `setFinalLongPayout` with a "not already finalized" check.
- Alternatively, make `IncrementGeneration` accept an idempotency key tied to the `pending_transactions.id` so repeated recovery passes are safe no-ops rather than additional increments.

### Proof of Concept
1. A normal user pushes to a repository routed through Praefect's transactional mutator path; the coordinator inserts a `pending_transactions` row and the primary/secondaries vote and commit (`internal/praefect/coordinator.go:475-498`, `:921-961`).
2. Praefect crashes or is restarted immediately after `IncrementGeneration` commits inside the finalizer but before `DeletePendingTransaction` runs — leaving the row in place.
3. On the next Praefect startup, `RecoverPendingTransactions` loads the leftover row and calls `recoverPendingTransaction`, which calls `rs.IncrementGeneration` again unconditionally (`internal/praefect/recovery.go:107-117`), exactly as demonstrated by the existing test `"IncrementGeneration always called even if generation already advanced"` (`internal/praefect/recovery_test.go:157-186`).
4. The repository's recorded generation is now ahead of what any replica actually contains for that push, breaking the invariant that `storage_repositories.generation == repositories.generation` implies full replication.

### Citations

**File:** internal/praefect/coordinator.go (L475-498)
```go
		// Write a crash-recovery record before any Gitaly node can commit changes. If Praefect
		// crashes between quorum and the finalizer, this record lets startup recovery
		// detect and reconcile the inconsistency.
		var pendingTxID int64
		if c.pts != nil && change == datastore.UpdateRepo {
			allVoters := make([]string, 0, 1+len(route.Secondaries))
			allVoters = append(allVoters, route.Primary.Storage)
			for _, s := range route.Secondaries {
				allVoters = append(allVoters, s.Storage)
			}

			id, err := c.pts.RecordPendingTransaction(ctx, datastore.PendingTransaction{
				RepositoryID:   route.RepositoryID,
				VirtualStorage: virtualStorage,
				RelativePath:   targetRepo.GetRelativePath(),
				ReplicaPath:    route.ReplicaPath,
				PrimaryStorage: route.Primary.Storage,
				ExpectedVoters: allVoters,
			})
			if err != nil {
				return nil, fmt.Errorf("record pending transaction for crash recovery: %w", err)
			}
			pendingTxID = id
		}
```

**File:** internal/praefect/coordinator.go (L948-961)
```go
		if err := c.newRequestFinalizer(
			ctx, route.RepositoryID, virtualStorage, targetRepo, route.ReplicaPath, route.Primary.Storage,
			updated, outdated, change, params, cause, route.AdditionalReplicaPath)(); err != nil {
			return err
		}

		// Finalizer succeeded: the generation has been incremented. Safe to remove the crash-recovery record.
		if c.pts != nil && pendingTxID != 0 {
			if err := c.pts.DeletePendingTransaction(ctx, pendingTxID); err != nil {
				c.logger.WithError(err).WithField("transaction_id", pendingTxID).WarnContext(ctx, "failed to delete pending transaction record")
			}
		}
		return nil
	}
```

**File:** internal/praefect/recovery.go (L107-117)
```go
	// By advancing the generation here without a mechanism to check if
	// it was already advanced, for example due to a race, can cause it
	// to be over-advanced. But this is known and since consistent nodes
	// advance together, reads remain correct. And all out-of-date nodes
	// would also get the same generation during replication. Therefore,
	// it is safe to always increment the generation here.
	if err := rs.IncrementGeneration(ctx, tx.RepositoryID, tx.PrimaryStorage, updated); err != nil {
		if !errors.Is(err, datastore.ErrWriteToOutdatedNodes) {
			return fmt.Errorf("increment generation: %w", err)
		}
	}
```

**File:** internal/praefect/recovery_test.go (L157-186)
```go
	t.Run("IncrementGeneration always called even if generation already advanced", func(t *testing.T) {
		primarySrv := &mockChecksumServer{checksum: checksumA}
		sec1Srv := &mockChecksumServer{checksum: checksumA}

		conns := Connections{vs: {
			primary: startChecksumServer(t, primarySrv),
			sec1:    startChecksumServer(t, sec1Srv),
		}}

		incrementCalled := false
		rs := datastore.MockRepositoryStore{
			IncrementGenerationFunc: func(_ context.Context, _ int64, _ string, _ []string) error {
				incrementCalled = true
				return nil
			},
		}
		queue := datastore.NewReplicationEventQueueInterceptor(datastore.NewMemoryReplicationEventQueue(config.Config{}))

		pts := &simpleMockPTS{records: []datastore.PendingTransaction{{
			ID: 2, RepositoryID: repoID, VirtualStorage: vs,
			RelativePath: relPath, ReplicaPath: repPath,
			PrimaryStorage: primary,
			ExpectedVoters: []string{primary, sec1},
		}}}

		require.NoError(t, RecoverPendingTransactions(ctx, pts, rs, queue, conns, testhelper.SharedLogger(t)))
		require.True(t, incrementCalled, "IncrementGeneration must always be called during recovery")
		require.Empty(t, queue.GetEnqueued())
		require.Equal(t, []int64{2}, pts.deleted)
	})
```
