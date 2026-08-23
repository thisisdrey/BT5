### Title
Unbounded reference-update loop in `UpdateReferences` allows client-driven DoS - (File: internal/gitaly/service/ref/update_references.go)

### Summary
`RefService.UpdateReferences` is a client-streaming MUTATOR RPC that accepts an unbounded number of `UpdateReferencesRequest` messages, each carrying an arbitrary number of `Updates`. The handler loops over every update in every received message and queues it into a single `git-update-ref` transaction with no limit on the total number of updates, mirroring the "unbounded `for` loop over a caller-controlled list" pattern from the cited Yieldy `BatchRequests.sendWithdrawalRequests` finding.

### Finding Description
The handler reads messages in a `for { ... }` loop, and for each message iterates `request.GetUpdates()` unconditionally, calling `git.ValidateReference`, `objectHash.ValidateHex`, and `updater.Update(...)` for every single entry, with no cap on the number of updates per message nor on the number of messages in the stream: [1](#0-0) 

There is no `PaginationParameter`, `limit`, or any other bound analogous to what is enforced elsewhere in the codebase for potentially unbounded collections (e.g. `ListRefs`, `ListCommits`, `ListBlobs`, `ListAllCommits`, `ListPartitions` all implement explicit `limit`/pagination logic): [2](#0-1) [3](#0-2) 

By contrast, `UpdateReferences` has no such guard: each update is validated and queued via `updater.Update`, and the whole (potentially huge) set is committed at once via `updater.Commit()` — the equivalent of the vulnerable unbounded loop calling an external operation for every list element in the Yieldy report.

### Impact Explanation
An ordinary, authenticated Gitaly client (any caller permitted to invoke `RefService.UpdateReferences`, e.g. GitLab Rails performing batch ref operations) can submit a very large number of reference updates in a single logical call (spread across as many streamed messages as desired, since the RPC has no cap on message count either). Each update requires hex/format validation and is queued into a single `git-update-ref` transaction, whose commit cost (locking, hashing, and reference-transaction hook execution/voting per `doc/hooks.md`) grows with the number of updates. A sufficiently large update set can consume disproportionate CPU/memory on the Gitaly node and stall other work on that repository or storage during the single large `updater.Commit()` call, producing a resource-exhaustion/DoS condition — the same class of unbounded-iteration DoS described in the report, just materializing as CPU/latency exhaustion in a single-repository RPC handler instead of gas exhaustion in a smart contract loop.

### Likelihood Explanation
Likelihood is moderate: the RPC is a `MUTATOR` intended for legitimate batch reference changes (used internally by the backup restore path via `sendRefUpdates`), so it is reachable by any caller authorized to mutate the target repository — no privileged Gitaly-admin or off-path/MITM position is required, matching the "unprivileged" criterion. However, exploitation requires the caller to already have write access to the repository and to intentionally submit an abnormally large update set, so it is not exploitable by a fully anonymous/read-only actor, and gRPC's default message size limits and general concurrency limiting middleware may partially mitigate severity, similar to how the original finding was only acknowledged/kept as Medium because the array was owner-gated.

### Recommendation
Add an explicit maximum count for the number of `Updates` accepted per RPC invocation (across all streamed messages), returning `InvalidArgument` once the limit is exceeded, similar to the `limit`/pagination patterns already used by `ListRefs`, `ListCommits`, and `ListBlobs`. Alternatively, batch large update sets into multiple bounded `git-update-ref` transactions (as `updateRemoteMirror` already does via `pushBatchSize`, see `internal/gitaly/service/remote/update_remote_mirror.go:225-242`) so that a single request cannot force one unbounded, monolithic reference-transaction commit.

### Proof of Concept
1. Open an `UpdateReferences` stream against a repository the caller has write access to.
2. Send an initial request with `Repository` set and one `Update`.
3. Send subsequent requests each containing a very large `Updates` array (e.g. hundreds of thousands of valid or minimally-valid entries), or send an extremely large number of subsequent stream messages — the handler loop at [4](#0-3)  places no bound on `len(request.GetUpdates())` or on the number of iterations of the outer `for {}` loop.
4. Close the stream; `updater.Commit()` is invoked over the full accumulated update set, consuming CPU/IO proportional to the (attacker-controlled) size, degrading the Gitaly node's ability to service other requests against that repository/storage.

### Citations

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

**File:** internal/gitaly/service/commit/list_all_commits.go (L69-77)
```go
	limit := request.GetPaginationParams().GetLimit()
	parser := catfile.NewParser()

	for i := int32(0); catfileObjectIter.Next(); i++ {
		// If we hit the pagination limit, then we stop sending commits even if there are
		// more commits in the pipeline.
		if limit > 0 && limit <= i {
			break
		}
```

**File:** internal/gitaly/service/partition/list_partitions.go (L23-55)
```go
	paginationParams := in.GetPaginationParams()
	startPartitionID := invalidPartitionID
	pageLimit := 100
	var err error
	if paginationParams != nil {
		pageLimit = int(paginationParams.GetLimit())
		startPartitionID, err = decodePageToken(paginationParams)
		if err != nil {
			return nil, structerr.NewInvalidArgument("invalid page token: %w", err)
		}
	}

	storageHandle, err := s.node.GetStorage(in.GetStorageName())
	if err != nil {
		return nil, fmt.Errorf("get storage: %w", err)
	}

	it, err := storageHandle.ListPartitions(startPartitionID)
	if err != nil {
		return nil, err
	}
	defer it.Close()

	var partitions []*gitalypb.Partition
	for it.Next() {
		partitions = append(partitions, &gitalypb.Partition{
			Id: it.GetPartitionID().String(),
		})

		if len(partitions) >= pageLimit {
			break
		}
	}
```
