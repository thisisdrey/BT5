### Title
Unbounded reference-update queue in `UpdateReferences` allows resource-exhaustion DoS - ([File: internal/gitaly/service/ref/update_references.go])

### Summary
The `UpdateReferences` RPC handler accepts a client-streamed sequence of `UpdateReferencesRequest` messages, each carrying a `repeated Update updates` field, and queues every entry into an underlying `git update-ref` process without any bound on the total number of updates that may be queued across the whole stream, analogous to the reported `massUpdatePools()` issue where a loop's cost is driven entirely by an attacker/caller-controlled collection length with no cap.

### Finding Description
`UpdateReferences` receives an initial request, builds an `updateref.Updater`, and then loops `for { ... }`, calling `server.Recv()` repeatedly until `io.EOF`. On each iteration it validates and queues every element of `request.GetUpdates()` via `updater.Update(...)`, with no limit on the number of `Update` messages that can appear in a single `UpdateReferencesRequest.updates` field, nor any limit on the number of stream messages the client may send before finishing with EOF. [1](#0-0) 

The only guard present is a check that `len(request.GetUpdates()) != 0` (rejecting empty batches), not an upper bound: [2](#0-1) 

Each accepted update is queued into a single `updateref.Updater` instance backed by a `git update-ref --stdin` process, and all queued updates are only finally committed once at the end of the stream via `updater.Commit()`: [3](#0-2) [4](#0-3) 

Because gRPC's per-message size limit only bounds the size of a single `UpdateReferencesRequest`, it does not bound the aggregate size of the whole client stream. A caller can send an arbitrarily large number of stream messages (each within the message-size limit but individually cheap to construct), each containing many `Update` entries, causing Gitaly to hold a single long-lived `git update-ref` process open, accumulate an unbounded amount of pending reference-update state, and perform validation work (`git.ValidateReference`, hex validation for two object IDs) per entry for the entire duration of the RPC before a single commit/lock is taken. This mirrors the reported bug class ("loop that depends on [an externally-controlled] length" with no bound), mapped here to Gitaly's "RPC-handler resource limits" analog.

### Impact Explanation
An ordinary client authorized to call `UpdateReferences` (used internally by GitLab, e.g., for batch ref housekeeping/migrations as seen in `_support/benchmarking/incluster/provision-repos`) can submit an extremely long-lived stream with an unbounded number of updates. This ties up a Gitaly worker goroutine, an open `git update-ref` subprocess, and unbounded server-side memory/state for the lifetime of the call, and defers all lock acquisition/validation cost to a single RPC invocation whose size is entirely attacker/caller-controlled. This can starve the repository's reference-update path (single writer lock semantics of `git update-ref`) and consume Gitaly server resources, resulting in a denial-of-service against the ref-update handler for that repository (and potentially the node under concurrent abuse). It does not directly lead to unauthorized data access, but is a legitimate handler resource-limit gap.

### Likelihood Explanation
Likelihood is moderate: `UpdateReferences` is an authenticated, direct RPC that legitimate GitLab/Gitaly clients (or anyone with valid Gitaly gRPC access/token, e.g. via Praefect-routed calls) can call. No special repository state or privilege escalation is needed — only the ability to open a client-streaming gRPC call against the `RefService`. The benchmarking tooling in-repo itself demonstrates that large volumes of ref updates (hundreds of thousands) are expected as legitimate use, showing the RPC is designed for potentially large batches without protective bounding, making accidental or intentional resource exhaustion plausible.

### Recommendation
Add an explicit cap on the total number of `Update` entries accepted per `UpdateReferences` call (summed across all streamed messages), rejecting the call with `InvalidArgument` once the limit is exceeded, similar to the pagination/`limit` parameters already used elsewhere in the `ref` service (e.g. `FindRefsByOID`'s `Limit` field, `ListRefs`'s `PaginationParameter`). Alternatively, document and enforce a maximum batch size, and/or require clients to split very large update sets into multiple independent RPC calls with intermediate commits, bounding the lifetime of any single `git update-ref` subprocess and the memory held per call.

### Proof of Concept
1. Open a client-streaming call to `RefService.UpdateReferences`.
2. Send an initial `UpdateReferencesRequest` with a valid `Repository`.
3. In a loop, send thousands/millions of subsequent `UpdateReferencesRequest` messages, each containing a large `Updates` array of valid-looking (or minimally valid) reference updates (no repository field set, per protocol).
4. Observe that Gitaly's `UpdateReferences` handler (`internal/gitaly/service/ref/update_references.go`) queues every single update via `updater.Update(...)` without ever rejecting the request for volume, keeping the underlying `git update-ref --stdin` process and goroutine alive and accumulating state for the full duration of the stream, consuming increasing CPU/memory/file-descriptor resources on the Gitaly node until the stream ends or resources are exhausted.

### Citations

**File:** internal/gitaly/service/ref/update_references.go (L38-45)
```go
	updater, err := updateref.New(ctx, repo)
	if err != nil {
		return fmt.Errorf("creating updater: %w", err)
	}

	if err := updater.Start(); err != nil {
		return fmt.Errorf("starting updater: %w", err)
	}
```

**File:** internal/gitaly/service/ref/update_references.go (L47-97)
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

		if request, err = server.Recv(); err != nil {
			if errors.Is(err, io.EOF) {
				break
			}

			return fmt.Errorf("receiving subsequent request: %w", err)
		}
	}
```

**File:** internal/gitaly/service/ref/update_references.go (L99-132)
```go
	if err := updater.Commit(); err != nil {
		var alreadyLockedErr updateref.AlreadyLockedError
		if errors.As(err, &alreadyLockedErr) {
			return structerr.NewAborted("%w", err).
				WithDetail(&gitalypb.UpdateReferencesError{
					Error: &gitalypb.UpdateReferencesError_ReferencesLocked{
						ReferencesLocked: &gitalypb.ReferencesLockedError{
							Refs: [][]byte{[]byte(alreadyLockedErr.ReferenceName)},
						},
					},
				})
		}

		var mismatchingStateErr updateref.MismatchingStateError
		if errors.As(err, &mismatchingStateErr) {
			return structerr.NewAborted("%w", err).
				WithDetail(&gitalypb.UpdateReferencesError{
					Error: &gitalypb.UpdateReferencesError_ReferenceStateMismatch{
						ReferenceStateMismatch: &gitalypb.ReferenceStateMismatchError{
							ReferenceName:    []byte(mismatchingStateErr.ReferenceName),
							ExpectedObjectId: []byte(mismatchingStateErr.ExpectedObjectID),
							ActualObjectId:   []byte(mismatchingStateErr.ActualObjectID),
						},
					},
				})
		}

		var nonExistentObjectErr updateref.NonExistentObjectError
		if errors.As(err, &nonExistentObjectErr) {
			return structerr.NewNotFound("%w", err)
		}

		return fmt.Errorf("committing update: %w", err)
	}
```
