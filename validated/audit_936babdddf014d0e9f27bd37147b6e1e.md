### Title
Unbounded `UpdateReferences` stream allows resource-exhaustion DoS via unlimited reference updates - (File: `internal/gitaly/service/ref/update_references.go`)

### Summary
The `UpdateReferences` RPC accepts a client-streamed sequence of reference-update batches with no upper bound on the number of updates a client may enqueue in a single transaction. Each update is validated and forwarded one-by-one to a `git update-ref --stdin` subprocess, and the same unbounded set is later hashed for reference-transaction voting. A caller who can invoke this RPC (any client authorized to call the Gitaly service, e.g., via the normal push/rewrite path or a direct RPC call) can submit an arbitrarily large number of updates in one streaming call, forcing Gitaly to iterate over and buffer the entire set before it can be validated or rejected. This mirrors the reported bug class: unbounded iteration over an attacker-influenced collection with no length cap, which drives resource consumption (here CPU/memory/time) to the point of failure — the Gitaly analog of "out-of-gas."

### Finding Description
`UpdateReferences` loops over `server.Recv()` until `io.EOF`, and for every request it iterates `request.GetUpdates()` without ever checking a maximum count of total updates queued into the `updateref.Updater`: [1](#0-0) 

Nothing in `updateref.New`/`Update`/`Commit` enforces a cap on the number of staged updates; `Update` merely writes another `update ...` line to the `git update-ref --stdin` process for every call: [2](#0-1) 

Each staged update also triggers Gitaly's reference-transaction hook machinery, which reads the entire list of changes from stdin and parses/hashes them for voting on every prepare/commit phase: [3](#0-2) 

and, in a transactional storage backend, every update passes through `Transaction.UpdateReferences`, which does further per-reference map lookups and nested loops over previously staged updates for conflict resolution: [4](#0-3) 

Because `UpdateReferences` is a client-streaming RPC, gRPC's per-message size limit does not bound the total size of the transaction — a client can send an effectively unlimited number of small `UpdateReferencesRequest` messages, each containing many `Update` entries, before ever calling `Commit()`. There is no accumulated-count check comparable to a "max cancellation list length" cap recommended in the referenced report.

### Impact Explanation
An attacker (any actor with legitimate access to call Gitaly's `RefService.UpdateReferences`, e.g., through normal repository write access) can submit a very large number of reference updates in a single streaming RPC call. This forces the Gitaly node to:
- Validate and buffer every update (`git.ValidateReference`, `objectHash.ValidateHex`) before any commit decision is made.
- Feed an unbounded stream of commands into the `git update-ref --stdin` subprocess.
- Hash and parse the full change set in the reference-transaction hook for every vote phase (prepare/committed), which is O(n) or worse in the number of updates.

This can exhaust CPU, memory, or wall-clock time on the Gitaly node, potentially timing out the RPC, starving other clients of the git-update-ref/quorum-voting machinery, or causing the process to be OOM-killed — a denial-of-service of the ref-update handler, directly analogous to the out-of-gas DoS in the referenced report.

### Likelihood Explanation
Likelihood is moderate: it requires only the ability to invoke a standard, non-privileged Gitaly RPC (`UpdateReferences`), something any client with write access to a repository can do (this RPC underlies GitLab's atomic ref-update flows). No admin/operator privilege, leaked credential, or malicious peer/node role is required — a single misbehaving or compromised ordinary client is sufficient to trigger the condition.

### Recommendation
Enforce a maximum number of staged reference updates per `UpdateReferences` transaction (and/or an aggregate size limit across all streamed requests), rejecting the RPC early with `InvalidArgument` once the limit is exceeded, similar to limits already used elsewhere in Gitaly (e.g., `blobsLimit`, `bytesLimit` in `ListBlobs`). Consider also bounding how much data the reference-transaction hook will parse and hash per invocation to avoid unbounded CPU cost during voting.

### Proof of Concept
1. Open an `UpdateReferences` stream to a Gitaly node the attacker has push/write access to.
2. Send the first request with `repository` set and no or few updates.
3. Continue streaming a very large number of subsequent `UpdateReferencesRequest` messages, each containing many well-formed `Update` entries (valid ref names/OIDs so early validation doesn't reject them), without calling half-close/`Commit`.
4. Observe Gitaly buffering and queuing all updates into the `git update-ref --stdin` subprocess and, upon commit, executing the reference-transaction hook logic, consuming increasing CPU/memory proportional to the number of updates, until the RPC times out or the node becomes unresponsive to other requests.

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

**File:** internal/gitaly/hook/referencetransaction.go (L40-102)
```go
	changes, err := io.ReadAll(stdin)
	if err != nil {
		return fmt.Errorf("reading stdin from request: %w", err)
	}

	var tx storage.Transaction
	if payload.TransactionID > 0 {
		tx, err = m.txRegistry.Get(payload.TransactionID)
		if err != nil {
			return fmt.Errorf("get transaction: %w", err)
		}
	}

	var phase voting.Phase
	switch state {
	// We're voting in preparing state to tell Praefect that the reference changes are ready,
	// although they are not locked yet. At this stage, we can serialize write transactions by
	// acquiring a lock for the transaction and releasing it only when the transaction is
	// committed or aborted. Other transactions must wait for the lock.
	case ReferenceTransactionPreparing:
		phase = voting.Preparing
	// We're voting in prepared state to tell Praefect we've locked the reference changes and
	// queued them for the transaction. We can abort the transaction if needed.
	case ReferenceTransactionPrepared:
		phase = voting.Prepared

		if tx != nil {
			updates, err := parseChanges(objectHash, bytes.NewReader(changes))
			if err != nil {
				return fmt.Errorf("parse changes: %w", err)
			}

			initialValues := map[git.ReferenceName]git.Reference{}
			for reference, update := range updates {
				if update.OldOID != "" {
					initialValues[reference] = git.NewReference(reference, update.OldOID)
				} else {
					initialValues[reference] = git.NewSymbolicReference(reference, update.OldTarget)
				}
			}

			// Only record the initial values of the reference in the prepare step as this
			// change hasn't yet been committed.
			if err := tx.RecordInitialReferenceValues(ctx, initialValues); err != nil {
				return fmt.Errorf("record initial reference value: %w", err)
			}
		}
	// We're also voting in committed state to tell Praefect we've actually persisted the
	// changes. This is necessary as some RPCs fail return errors in the response body rather
	// than as an error code. Praefect can't tell if these RPCs have failed. Voting on committed
	// ensure Praefect sees either a missing vote or that the RPC did commit the changes.
	case ReferenceTransactionCommitted:
		phase = voting.Committed

		if tx != nil {
			updates, err := parseChanges(objectHash, bytes.NewReader(changes))
			if err != nil {
				return fmt.Errorf("parse changes: %w", err)
			}

			if err := tx.UpdateReferences(ctx, updates); err != nil {
				return fmt.Errorf("update references: %w", err)
			}
```

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager.go (L701-749)
```go
func (txn *Transaction) UpdateReferences(ctx context.Context, updates git.ReferenceUpdates) error {
	u := git.ReferenceUpdates{}

	for reference, update := range updates {
		// Transactions should only stage references with valid names as otherwise Git would already
		// fail when they try to stage them against their snapshot. `update-ref` happily accepts references
		// outside of `refs` directory so such references could theoretically arrive here. We thus sanity
		// check that all references modified are within the refs directory.
		//
		// HEAD is a special case and refers to a default branch update.
		if !strings.HasPrefix(reference.String(), "refs/") && reference != "HEAD" {
			return InvalidReferenceFormatError{ReferenceName: reference}
		}

		oldOID := update.OldOID
		oldTarget := update.OldTarget

		if initialValue, ok := txn.initialReferenceValues[reference]; ok {
			if !initialValue.IsSymbolic {
				oldOID = git.ObjectID(initialValue.Target)
			} else {
				oldTarget = git.ReferenceName(initialValue.Target)
			}
		}

		if oldOID == update.NewOID && oldTarget == update.NewTarget {
			// This was a no-op.
			continue
		}

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

		u[reference] = git.ReferenceUpdate{
			OldOID:    oldOID,
			NewOID:    update.NewOID,
			OldTarget: oldTarget,
			NewTarget: update.NewTarget,
		}
	}
```
