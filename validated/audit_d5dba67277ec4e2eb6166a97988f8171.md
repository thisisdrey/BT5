Based on my analysis, I've identified a valid analog vulnerability in the `UpdateReferences` RPC.

### Title
Unbounded number of reference updates in `UpdateReferences` RPC allows DoS of the underlying `git-update-ref` transaction - (File: internal/gitaly/service/ref/update_references.go)

### Summary
The `UpdateReferences` RPC handler accepts a client-controlled, streamed sequence of reference updates with no limit on the total count, queueing every one of them into a single `git update-ref --stdin` transaction. An unprivileged client can submit an arbitrarily large number of updates in one atomic transaction, causing the transaction's `commit` phase (including the mandatory reference-transaction hook used for voting) to become extremely expensive, potentially exceeding gRPC/session deadlines or exhausting Gitaly node resources — the same "unbounded input to a bulk mutating operation" pattern described in the source report, where an unbounded number of `offers` allowed order creation to succeed while the corresponding stop/settlement operation could then exceed a hard resource limit.

### Finding Description
`server.UpdateReferences` reads a client stream and, for every request message, iterates `request.GetUpdates()` and calls `updater.Update()` for each one, with no cap on the number of updates across the whole stream: [1](#0-0) 

Each `Update()` call simply validates the individual reference name/OID and writes a corresponding `update <ref>\0<new>\0<old>\0` command to the underlying `git update-ref --stdin` process, again with no limit on the number of queued commands: [2](#0-1) 

When the stream ends, the handler implicitly relies on the caller (or a wrapping mutator transaction) to call `Commit()`, which sends the `commit` state-transition to `git-update-ref` and blocks on reading back its "ok" response: [3](#0-2) 

`updateref.New` always attaches the reference-transaction hook (`gitcmd.WithRefTxHook`) unless transactions are explicitly disabled, meaning every single queued reference update triggers hook/voting logic during commit: [4](#0-3) 

Because there is no limit anywhere in this call path (RPC handler, `Updater.Update`, or the hook invocation) on the number of `updates` in a transaction, a single streaming RPC call can queue hundreds of thousands or millions of ref updates. This mirrors the reported bug class: no limit on the size of a bulk operation input allows the "cheap" create/queue phase to succeed, while the corresponding processing/commit phase becomes disproportionately expensive and can hit a hard external resource limit (there, the block gas limit; here, gRPC/context deadlines, hook-invocation overhead multiplied per update, and the underlying process's own resource ceiling).

### Impact Explanation
An attacker with only push/write access to a single repository (an ordinary Gitaly client capability, not a privileged actor) can craft one `UpdateReferences` call with an extremely large number of updates. The resulting `git update-ref --stdin` transaction must lock, hook-vote, and write every reference in one atomic operation. If this transaction times out or is killed due to exceeding available CPU/time/deadline budgets, the server-side git process may be aborted mid-transaction, and worse, if a large in-flight transaction consumes excessive per-request CPU/memory, it degrades or denies service for other RPCs to the same repository/storage (a DoS of the `RefService` handler), since Gitaly's admission/concurrency limiting is per-RPC/queue based rather than bounded by request payload size (see `internal/limiter/concurrency_limiter.go`), not by the number of items inside a single accepted request.

### Likelihood Explanation
Likelihood is moderate-to-high: `UpdateReferences` is a `MUTATOR`-scoped RPC reachable by any client authorized to push/write to a repository (e.g., via a corresponding Rails/Workhorse-mediated push path, or directly if raw gRPC access is available), and the request is a simple streamed protobuf message with a `repeated Update updates` field that has no documented or enforced maximum size: [5](#0-4) 

No validation rejects large update counts before they are queued into the single-process `git-update-ref` transaction.

### Recommendation
Enforce a maximum number of updates per `UpdateReferences` transaction (mirroring the analogous fix recommended in the source report — capping the number of `offers` per order). Concretely:
- Add an explicit limit (e.g., a few thousand updates) enforced inside the loop in `server.UpdateReferences` (`internal/gitaly/service/ref/update_references.go`), returning `structerr.NewInvalidArgument` once the running total of queued updates for the stream exceeds the limit.
- Alternatively/additionally, enforce the same cap inside `updateref.Updater.Update` (`internal/git/updateref/updateref.go`) so all callers of the `Updater` API benefit, not just the RPC handler.

### Proof of Concept
1. Open a client-side `UpdateReferences` stream against a repository the attacker can write to.
2. Send the first request with `Repository` set and a small batch of `updates`.
3. Send many additional stream chunks (each within gRPC per-message size limits) whose combined `updates` count is extremely large (e.g., hundreds of thousands of `refs/heads/x-<n>` create operations).
4. Close the stream and observe that the server accumulates all updates into a single `git update-ref --stdin` transaction and calls `Commit()`, causing the RPC to run for a disproportionately long time or fail after consuming significant CPU/memory, analogous to the "cheap create, expensive settle" DoS pattern from the source report.

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

**File:** internal/git/updateref/updateref.go (L303-331)
```go
	objectHash, err := repo.ObjectHash(ctx)
	if err != nil {
		return nil, fmt.Errorf("detecting object hash: %w", err)
	}

	txOption := gitcmd.WithRefTxHook(objectHash, repo)
	if cfg.disableTransactions {
		txOption = gitcmd.WithDisabledHooks()
	}

	cmdFlags := []gitcmd.Option{gitcmd.Flag{Name: "-z"}, gitcmd.Flag{Name: "--stdin"}}
	if cfg.noDeref {
		cmdFlags = append(cmdFlags, gitcmd.Flag{Name: "--no-deref"})
	}

	var stderr bytes.Buffer
	cmd, err := repo.Exec(ctx,
		gitcmd.Command{
			Name:  "update-ref",
			Flags: cmdFlags,
		},
		txOption,
		gitcmd.WithSetupStdin(),
		gitcmd.WithSetupStdout(),
		gitcmd.WithStderr(&stderr),
	)
	if err != nil {
		return nil, err
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

**File:** proto/ref.proto (L344-368)
```text
// UpdateReferencesRequest is a request for the UpdateReferences RPC.
message UpdateReferencesRequest {
  // Update represents a reference update.
  message Update {
    // reference is the fully-qualified reference name that should be updated.
    bytes reference = 1;

    // old_object_id is the object ID the reference should be pointing to in order to be updated.
    // This has the intent to avoid time-of-check-time-of-use-style races when the object ID
    // has changed.
    //
    // If empty, the reference will be force-updated without any such checks. If set to the
    // all-zeroes object ID, this will verify that the branch did not exist previously.
    bytes old_object_id = 2;

    // new_object_id is the object ID the reference should be updated to. If set to the all-zeroes
    // object ID the branch will be deleted.
    bytes new_object_id = 3;
  };

  // repository is the repository where references shall be updated in.
  Repository repository = 1 [(target_repository)=true];
  // updates is the set of reference updates that shall be performed.
  repeated Update updates = 2;
}
```
