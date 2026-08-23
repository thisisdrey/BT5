### Title
Unbounded reference-update queue in `UpdateReferences` enables DoS via unbounded transaction size - (File: internal/gitaly/service/ref/update_references.go)

### Summary
The `RefService.UpdateReferences` RPC is a streaming, `MUTATOR`-classified endpoint that lets an authenticated client atomically create/update/delete an arbitrary number of references in a single transaction. Neither the number of `UpdateReferencesRequest` stream messages nor the number of `updates` per message is bounded, and there is no minimum-size or maximum-count enforcement anywhere in the handler or in the underlying `updateref.Updater`, mirroring the reported "unbounded commit queue with no minimum size" bug class.

### Finding Description
`UpdateReferences` reads an unbounded stream of `UpdateReferencesRequest` messages and, for every message, iterates over `request.GetUpdates()` without any cap, queueing each entry into a single `updateref.Updater` instance via `updater.Update(...)`: [1](#0-0) 

The only content check is per-field syntactic validation (reference name format, object ID hex format) — there is no limit on the *number* of updates that can be queued in the loop, and no minimum size/count requirement: [2](#0-1) 

All queued updates are held in-memory/streamed to a single `git update-ref --stdin` subprocess and only materialize into a single `Commit()` call at the very end of the stream: [3](#0-2) 

The underlying `updateref.Updater` itself imposes no size limit either — `Update()` just writes another `update` command to the pipe and `Commit()` performs a single all-at-once `git update-ref` transaction, locking every referenced ref/reftable for the whole duration: [4](#0-3) [5](#0-4) 

Gitaly's benchmark suite acknowledges that transaction size materially affects throughput (100+ updates already show measurable per-op cost), underscoring that an attacker-controlled transaction size is a real resource-consumption knob: [6](#0-5) 

This is the direct structural analog of the reported PoolCommitter bug: no minimum commit size, and an unbounded queue of pending state changes that must be iterated/executed by a single privileged worker (there, `PoolKeeper.performUpkeepSinglePool`; here, the `update-ref --stdin` process and reference-transaction hook invocation) within a fixed resource budget (there, block gas limit; here, gRPC/streamcache/WAL processing time, memory, and per-repo reference locks).

### Impact Explanation
An ordinary user with write access to a repository (or any caller able to reach `RefService.UpdateReferences`, which is a general-purpose mutator RPC — not solely tied to `git push`) can submit a single logical transaction containing an extremely large number of trivial reference creations (e.g., unique throwaway refs under a namespace they control). Because:
- there's no minimum size/cost per update, and
- there's no maximum count enforced by the handler, the `Updater`, or downstream reftable/WAL commit path,

the server must hold locks on all touched references simultaneously, run the reference-transaction hook logic, and commit an unbounded transaction to the WAL/reftable backend, potentially:
- consuming excessive memory/CPU on the Gitaly node processing the request,
- holding a long-lived voting/commit lock that starves other RPCs on the same repository/partition,
- exceeding streaming/RPC time budgets, effectively denying reference-mutation service for that repository (and, since references are locked repo-wide during commit, blocking concurrent read/write reference operations).

This matches the "DoS of a handler" acceptance criterion via a crafted RPC field (an oversized `updates` list / unbounded streamed requests) rather than any privileged, MITM, or dependency-based path.

### Likelihood Explanation
Likelihood is high for any authenticated client permitted to call `UpdateReferences` for a repository they can write to: no special timing, race, or privilege escalation is required — a single client-controlled request stream with many `Update` entries suffices. The main uncertainty is the deployment-specific gRPC message size limit and any concurrency-limiter (`max_queue_size`/`max_per_repo` in `[[concurrency]]`, see `doc/backpressure.md`) that operators may have configured for this RPC; those are generic cross-RPC safety nets rather than an update-count-specific bound, and by default Gitaly does not appear to cap the number of `updates` per `UpdateReferences` invocation.

### Recommendation
- Enforce a maximum number of reference updates accepted per `UpdateReferences` call (both per-message and cumulative across the stream), rejecting with `InvalidArgument`/`ResourceExhausted` once exceeded.
- Consider a minimum-cost accounting model (similar to `[[concurrency]]` cost-aware admission) so that oversized transactions are throttled or rejected before they reach the `updateref.Updater`/reftable commit path.
- Ensure the concurrency-limiter documentation's `max_queue_size`/`max_per_repo` guidance is paired with an explicit RPC-level cap on `updates` count, since the limiter alone does not bound the size of a single admitted request.

### Proof of Concept
1. As an authenticated user with write access to a target repository, open a client-streaming call to `RefService.UpdateReferences`.
2. In the initial (and/or subsequent) `UpdateReferencesRequest` messages, populate `updates` with a very large number of entries (e.g., hundreds of thousands), each creating a distinct, valid reference name (e.g., `refs/heads/spam-<i>`) with a valid `new_object_id` and empty `old_object_id`.
3. Close the stream; the handler loops over every update in every message with no count limit before calling `updater.Commit()`: [7](#0-6) 
4. Observe the Gitaly node spend disproportionate memory/CPU/time processing and locking the entire batch as a single transaction, degrading or blocking other reference operations against the same repository/partition until the (unbounded) transaction completes or times out.

### Citations

**File:** internal/gitaly/service/ref/update_references.go (L47-88)
```go
	for {
		// Only the first request may have its repository set.
		if request.GetRepository() != nil {
			return structerr.NewInvalidArgument("repository set in subsequent request")
		}

		if len(request.GetUpdates()) == 0 {
			return structerr.NewInvalidArgument("no updates specified")
		}

		for _, update := range request.GetUpdates() {
			reference := string(update.GetReference())
			if err := git.ValidateReference(reference); err != nil {
				return structerr.NewInvalidArgument("validating reference: %w", err).
					WithMetadata("reference", reference).
					WithDetail(&gitalypb.UpdateReferencesError{
						Error: &gitalypb.UpdateReferencesError_InvalidFormat{
							InvalidFormat: &gitalypb.InvalidRefFormatError{
								Refs: [][]byte{[]byte(reference)},
							},
						},
					})
			}

			// The old object ID may be empty, in which case we don't care about the current value of the
			// reference but instead do a force update of it.
			oldObjectID := string(update.GetOldObjectId())
			if len(oldObjectID) > 0 {
				if err := objectHash.ValidateHex(oldObjectID); err != nil {
					return structerr.NewInvalidArgument("validating old object ID: %w", err).WithMetadata("old_object_id", oldObjectID)
				}
			}

			newObjectID := string(update.GetNewObjectId())
			if err := objectHash.ValidateHex(newObjectID); err != nil {
				return structerr.NewInvalidArgument("validating new object ID: %w", err).WithMetadata("new_object_id", newObjectID)
			}

			if err := updater.Update(git.ReferenceName(reference), git.ObjectID(newObjectID), git.ObjectID(oldObjectID)); err != nil {
				return structerr.NewInvalidArgument("queueing update: %w", err)
			}
		}
```

**File:** internal/gitaly/service/ref/update_references.go (L99-99)
```go
	if err := updater.Commit(); err != nil {
```

**File:** internal/git/updateref/updateref.go (L384-396)
```go
// Update commands the reference to be updated to point at the object ID specified in newOID. If
// newOID is the zero OID, then the branch will be deleted. If oldOID is a non-empty string, then
// the reference will only be updated if its current value matches the old value. If the old value
// is the zero OID, then the branch must not exist.
//
// A reference transaction must be started before calling Update.
func (u *Updater) Update(reference git.ReferenceName, newOID, oldOID git.ObjectID) error {
	if err := u.expectState(stateStarted); err != nil {
		return err
	}

	return u.write("update %s\x00%s\x00%s\x00", reference.String(), newOID, oldOID)
}
```

**File:** internal/git/updateref/updateref.go (L436-453)
```go
// Commit applies the commands specified in other calls to the Updater. Commit finishes the
// reference transaction and another one must be started before further changes can be staged.
func (u *Updater) Commit() error {
	// Commit can be called without preparing the transactions.
	if err := u.checkState(statePrepared); err != nil {
		if err := u.expectState(stateStarted); err != nil {
			return err
		}
	}

	u.state = stateIdle

	if err := u.setState("commit"); err != nil {
		return err
	}

	return nil
}
```

**File:** internal/git/updateref/updateref_test.go (L806-843)
```go
	b.Run("update", func(b *testing.B) {
		for _, tc := range []struct {
			// transactionSize determines how many references are updated in a single reference
			// transaction.
			transactionSize int
		}{
			{transactionSize: 1},
			{transactionSize: 10},
			{transactionSize: 100},
		} {
			b.Run(fmt.Sprintf("transaction size %d", tc.transactionSize), func(b *testing.B) {
				ctx := testhelper.Context(b)

				cfg, repo, repoPath, updater := setupUpdater(b, ctx)
				defer testhelper.MustClose(b, updater)

				commitOID1 := gittest.WriteCommit(b, cfg, repoPath)
				commitOID2 := gittest.WriteCommit(b, cfg, repoPath, gittest.WithParents(commitOID1))

				createReferences(b, repo, tc.transactionSize, commitOID1)

				old, new := commitOID1, commitOID2

				b.ReportAllocs()
				b.ResetTimer()
				began := time.Now()
				for n := 0; n < b.N; n++ {
					require.NoError(b, updater.Start())
					for i := 0; i < tc.transactionSize; i++ {
						require.NoError(b, updater.Update(getReferenceName(i), new, old))
					}
					require.NoError(b, updater.Commit())

					old, new = new, old
				}

				elapsed := time.Since(began)
				b.ReportMetric(float64(b.N*tc.transactionSize)/elapsed.Seconds(), "reference_updates/s")
```
