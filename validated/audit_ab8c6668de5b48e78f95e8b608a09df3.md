I found a strong candidate. In `internal/gitaly/service/ref/update_references.go`, the `UpdateReferences` RPC accepts a client-streamed list of `Update` entries per request and queues each into a single `git update-ref --stdin` transaction via `updater.Update()` without deduplicating by reference name across the whole stream.### Title
Unbounded, unlimited `Updates` array in `UpdateReferences` RPC allows a client to exhaust Gitaly resources in a single transaction - (File: internal/gitaly/service/ref/update_references.go)

### Summary
`RefService.UpdateReferences` is a client-streaming RPC that lets an unprivileged, authenticated caller (any client that can reach the RPC, e.g. via a push-adjacent workflow or direct RPC access) queue an arbitrary, unbounded number of reference updates into a single `git update-ref --stdin` transaction before ever calling `Commit`. Unlike the `CrowdFunding.investors` array in the referenced report, there is no natural cap on how many entries a single logical transaction can accumulate, and everything is buffered/queued synchronously inside one RPC handler invocation, mirroring the "unbounded array iterated in a single transaction" bug class.

### Finding Description
The handler reads a stream of `UpdateReferencesRequest` messages and, for every request, loops over `request.GetUpdates()` and calls `updater.Update(...)` for each entry, with no upper bound on the total number of updates accumulated across the whole stream: [1](#0-0) 

Each call to `updater.Update` simply writes a `update <ref>\x00<new>\x00<old>\x00` record to the `git update-ref --stdin` process's stdin pipe and returns immediately — there is no size or count validation performed by Gitaly itself: [2](#0-1) 

Because the RPC is client-streaming, the client can send an effectively unlimited number of `UpdateReferencesRequest` messages (each gRPC message is bounded, but the number of messages/streamed requests is not), each containing many `Update` entries, and Gitaly will keep queuing them into the same underlying `update-ref` process and, transitively, into the same reference-transaction hook invocation and vote computation, without ever rejecting the request for being "too large." All of this work happens before `Commit()` is ever invoked, so a client can hold a Gitaly worker (and the spawned `git update-ref` subprocess) busy accumulating state indefinitely.

This is the direct analog of the reported bug class: an externally supplied, repeated/array-like field is grown without bound and is fully processed within one transaction/handler execution, creating a resource-growth DoS vector, rather than proceeding page-by-page or being capped.

Regarding the "duplicate entries" half of the original report specifically: Git's `update-ref --stdin` machinery does independently reject two updates to the *same* reference name within one transaction (`MultipleUpdatesError` / `MultipleUpdatesRegex` in `internal/git/reference_backend.go`), so the direct "double payout" analog (same ref processed twice, effecting a double state change) is not reachable — Git itself errors out. The exploitable remainder of the bug class is therefore the *unbounded array / no dedup and no cap before processing* aspect, i.e. resource exhaustion of the RPC handler, not double-crediting.

### Impact Explanation
An attacker-controlled client can send a very large or effectively unbounded set of `Update` entries in one `UpdateReferences` call (across many streamed messages), causing:
- Sustained memory/CPU usage on the Gitaly node while the entries are serialized and piped to `git update-ref`.
- A long-lived `git update-ref --stdin` process and open reference-transaction hook invocation, tying up a concurrency slot for that repository (subject only to Gitaly's generic RPC concurrency-limiting configuration, not to any limit specific to this RPC's payload size).
- Because `UpdateReferences` is a `MUTATOR` RPC that participates in Praefect's transactional voting, an oversized update set also inflates the hashing/voting work done on every Gitaly replica in the cluster, amplifying the DoS impact from one client request to all voting nodes.

This matches "DoS of a handler" in the accepted-impact list. Severity is Medium: it requires no privilege beyond RPC access, but it does not lead to memory corruption, data loss, or cross-repository object access; it is a resource-exhaustion / availability issue bounded by existing generic concurrency limiting (`doc/backpressure.md`), which mitigates but does not eliminate the vector.

### Likelihood Explanation
Likelihood is Medium: reaching `UpdateReferences` only requires being an authenticated/authorized RPC caller (the same class of caller that can push or update refs through GitLab), there is no special crafted-object or timing requirement, and the vulnerable code path (looping over `request.GetUpdates()` with no cap) is on every request. The main mitigating factor is that generic `[[concurrency]]` backpressure settings (`max_per_repo`, `max_queue_size`) can limit blast radius if configured, but they do not bound the size of an individual accepted request/stream.

### Recommendation
Enforce an explicit, documented maximum number of `Update` entries (and/or total payload size) accepted per `UpdateReferences` call, validated incrementally as requests are streamed in `internal/gitaly/service/ref/update_references.go`, returning `InvalidArgument` once the limit is exceeded instead of continuing to queue unbounded work into the `git update-ref` transaction. This mirrors patterns already used for other bulk request types (e.g. paginated/streamed accessors) and prevents a single RPC invocation from growing unbounded system resource consumption.

### Proof of Concept
Not independently exploitable/verified in this static-analysis pass beyond code inspection; the flaw is structural (absence of a bound-check) rather than a specific crafted-input crash, so a concrete PoC would consist of a client streaming a very large number of `UpdateReferencesRequest.Updates` entries (e.g. hundreds of thousands of no-op or unique-ref updates) over one `UpdateReferences` call and observing sustained CPU/memory growth on the serving Gitaly node before `CloseSend`/`Commit`. This was not executed against a live instance as part of this review; confirming actual resource-exhaustion thresholds would require runtime testing with `internal/gitaly/service/ref/update_references_test.go` as a scaffold.

### Citations

**File:** internal/gitaly/service/ref/update_references.go (L43-88)
```go
	if err := updater.Start(); err != nil {
		return fmt.Errorf("starting updater: %w", err)
	}

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
