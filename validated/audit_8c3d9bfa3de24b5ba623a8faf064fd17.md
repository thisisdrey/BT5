Based on my investigation, the strongest reachable analog in this Gitaly codebase is the `RefService.UpdateReferences` streaming RPC, which lets an ordinary client queue an unbounded number of reference updates in a single logical transaction before it is ever committed or validated.

### Title
Unbounded reference-update queue in `UpdateReferences` RPC allows DoS of the reference transaction pipeline - (File: internal/gitaly/service/ref/update_references.go)

### Summary
`UpdateReferences` is a client-streaming RPC that receives repeated `UpdateReferencesRequest` messages and, for every reference contained in every message, immediately queues an `Updater.Update()` call with no bound on the total number of updates accepted before `Commit()` is invoked. This mirrors the reported bug class: a user-controlled loop pushes an unbounded number of items into a per-request array/queue (`erc721ByContract[...]` ↔ the update-ref stdin queue / in-memory `git.ReferenceUpdates` structures), which a later, more expensive step must fully process, creating a DoS vector against the handling of that request and its downstream consumers.

### Finding Description
In `UpdateReferences` (internal/gitaly/service/ref/update_references.go:47-96), the server loops over `stream.Recv()` messages indefinitely and, for each `update` in `request.GetUpdates()`, calls `updater.Update(...)` [1](#0-0) . There is no limit on the number of stream messages, nor on the number of updates per message, nor on the cumulative count of references queued in the `updateref.Updater` before `Commit()` is finally called at line 99 [2](#0-1) .

Each `Update()` call writes directly into the stdin of a spawned `git update-ref --stdin -z` process [3](#0-2) , and the process is only asked to actually commit once, at the very end, via `Commit()` → `setState("commit")` [4](#0-3) . Because the loop never validates a maximum count and the same gRPC stream can be kept open by the client indefinitely, an attacker can accumulate an arbitrarily large batch of pending reference updates for a single repository before the underlying `git-update-ref` process is asked to process, lock, and verify all of them at once.

This queued batch also feeds into `TransactionManager.UpdateReferences`, which appends the whole update map to `txn.referenceUpdates` and, for every reference, iterates over **all previously queued update batches** in the same transaction (`for _, updates := range txn.referenceUpdates`) [5](#0-4) . Combined with the unbounded ingestion in the RPC handler, this becomes an unbounded, attacker-controlled amount of per-reference work performed synchronously by a single request, directly analogous to the reported pattern where an unbounded array (pushed once, cheaply) is later walked in full by a costly downstream operation (liquidation ↔ commit/verification of the reference transaction).

### Impact Explanation
A single authenticated user issuing normal write RPCs (not requiring any special privilege beyond ordinary repository write access) can hold open an `UpdateReferences` stream and enqueue an extremely large number of reference updates for one repository, causing:
- Excessive memory consumption in Gitaly (the update queue, the spawned `git-update-ref` process's internal state, and the `TransactionManager`'s in-memory `referenceUpdates`/conflict-tracking structures).
- CPU/time exhaustion when `Commit()` finally asks `git-update-ref` to lock, verify, and apply all queued updates in one shot, and when the transaction manager subsequently packs objects for all corresponding reference tips (`heads` build-up in `packObjects`) [6](#0-5) .
- Potential resource-exhaustion-based DoS of the gRPC handler and its process, since there's no cap analogous to `pushBatchSize` used elsewhere in the codebase (e.g., `UpdateRemoteMirror` explicitly batches pushed refspecs to bound work per Git invocation) [7](#0-6) .

This is a legitimate, unprivileged-actor DoS candidate, though its severity is bounded by normal request timeouts and per-RPC resource limits that may already exist at the gRPC/transport layer (which I could not fully verify were present for this specific streaming RPC).

### Likelihood Explanation
Likelihood is high in the sense that triggering it requires only standard write access to a repository (an ordinary client capable of calling `UpdateReferences`), no race condition, and no privileged role — closely matching the "ordinary user" reachability required by the rules. However, I could not confirm in the available code whether there is an overarching gRPC message/stream size limit, request deadline, or repository-level reference-count cap enforced elsewhere in the stack (e.g., in Praefect or gRPC server interceptors) that would already mitigate this; this is a limitation of what I could verify via the index.

### Recommendation
Impose an explicit maximum number of reference updates that can be queued within a single `UpdateReferences` call (and/or within a single reference transaction more broadly), rejecting the request with `InvalidArgument` once the limit is exceeded, mirroring the batching pattern already used in `UpdateRemoteMirror` (`pushBatchSize`, `maxDivergentRefs`). This bounds both the `git-update-ref` invocation cost and the subsequent transaction-manager processing (conflict resolution, `packObjects`, WAL entry creation) to a predictable, safe upper limit.

### Proof of Concept
1. As an ordinary client with write access to a repository, open the `RefService.UpdateReferences` bidirectional stream.
2. Send a very large number of `UpdateReferencesRequest` messages (or few very large messages), each containing many `UpdateRequest.Updates` entries referencing distinct, previously non-existent reference names (e.g., `refs/heads/branch-<i>`), without ever closing the stream/sending EOF.
3. Each update is queued via `updater.Update()` with no bound (internal/gitaly/service/ref/update_references.go:57-88).
4. When the client finally closes the stream, `updater.Commit()` (line 99) forces Gitaly to process the entire accumulated batch at once inside `git-update-ref --stdin`, and the transaction manager must fold all of them into `referenceUpdates`/`packObjects`, consuming disproportionate CPU/memory relative to a single request — a resource-exhaustion DoS of the handling Gitaly process.

*Note: I was unable to verify from the indexed code whether an upstream gRPC size/timeout guard already exists specifically for this RPC's stream; this uncertainty should be confirmed with a live Devin session that can inspect the full gRPC server configuration and any Praefect-side interceptors.*

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

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager.go (L731-741)
```go
		for _, updates := range txn.referenceUpdates {
			if txUpdate, ok := updates[reference]; ok {
				if txUpdate.NewOID != "" {
					oldOID = txUpdate.NewOID
				}

				if txUpdate.NewTarget != "" {
					oldTarget = txUpdate.NewTarget
				}
			}
		}
```

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager.go (L1352-1367)
```go
	heads := make([]string, 0)
	for _, referenceUpdates := range transaction.referenceUpdates {
		for _, update := range referenceUpdates {
			if !update.IsRegularUpdate() {
				// We don't have to worry about symrefs here.
				continue
			}

			if update.NewOID == objectHash.ZeroOID {
				// Reference deletions can't introduce new objects so ignore them.
				continue
			}

			heads = append(heads, update.NewOID.String())
		}
	}
```

**File:** internal/gitaly/service/remote/update_remote_mirror.go (L20-26)
```go
const (
	// pushBatchSize is the maximum number of branches to push in a single push call.
	pushBatchSize = 10
	// maxDivergentRefs is the maximum number of divergent refs to return in UpdateRemoteMirror's
	// response.
	maxDivergentRefs = 100
)
```
