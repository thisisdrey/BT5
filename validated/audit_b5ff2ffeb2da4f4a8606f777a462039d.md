### Title
Unbounded Per-Reference Hook Fan-Out During Push Enables Push-Triggered Resource-Exhaustion DoS - ([File: internal/gitaly/hook/receivepack/receive_pack.go])

### Summary
An ordinary authenticated push can create an arbitrarily large number of reference updates in a single `git-receive-pack`/`proc-receive` transaction. Gitaly's receive-pack hook plumbing invokes the `update` hook — a full gRPC round-trip to the Hook Service (and any configured custom hook subprocess) — once per reference in the push, with no upper bound on the number of references a single push may contain. This mirrors the reported bug class: an attacker floods a single "transaction" with many cheap, small items, forcing the server-side matching/processing loop to fan out linearly (or worse, given per-item process/RPC overhead) until it exhausts a resource limit (there: block gas; here: CPU, goroutines, hook-subprocess slots, and RPC/transaction time), block­ing legitimate use of the same repository.

### Finding Description
`receivePackReferenceUpdates` iterates over every ref update contained in a push and, for each one, calls `hookManager.UpdateHook` (a gRPC call to the Hook Service that in turn may exec a custom `update` hook binary) before queueing the update: [1](#0-0) 

This loop has no size cap — `updates` is derived directly from the ref-update commands the pushing client sent in the wire protocol, so its length is fully attacker/user controlled. The `doc/hooks.md` execution-path diagram confirms the design: for `git-receive-pack`-driven pushes, gitaly-hooks execs "update" and calls the Hook Service **once per reference**, followed by a `reference-transaction` prepare/commit round-trip (each of which involves a vote through Praefect if configured): [2](#0-1) 

The lower-level `UpdaterWithHooks.UpdateReference` path used by `OperationService` RPCs likewise invokes `UpdateHook` per call with no batching limit: [3](#0-2) 

Nowhere in this pipeline is there a check that limits how many reference updates a single push/transaction may contain (unlike other Gitaly RPCs, which do enforce explicit result/limit parameters, e.g. `ForEachRef`'s `WithCount`, `ListBlobs`'s `blobsLimit`, or `UpdateRemoteMirror`'s `maxDivergentRefs`): [4](#0-3) [5](#0-4) [6](#0-5) 

Because Gitaly only guards concurrent *RPC* volume via the concurrency queue (`max_per_repo`, `max_queue_size`) rather than the *size* of work done inside a single accepted RPC, a single push containing thousands of tiny ref updates (e.g. new branches/tags) passes admission control as "one RPC" but then internally performs thousands of hook subprocess executions and gRPC round-trips, exactly analogous to the report's single "sell" transaction being forced to iterate thousands of tiny bid entries: [7](#0-6) 

### Impact Explanation
A push with a very large number of ref updates causes Gitaly (and Praefect, when voting is enabled) to perform a per-reference cascade of hook subprocess spawns, gRPC calls, and (in HA mode) consensus votes, all within one RPC invocation that is otherwise indistinguishable from a normal push by the concurrency limiter. This can:
- Consume disproportionate CPU/goroutines/process-spawn resources on the Gitaly node for the duration of the push, delaying or starving other RPCs against the same or other repositories on that node.
- Extend the "active push" reference-counted window (used to gate concurrent repository moves) for an unbounded time.
- In an HA/Praefect setup, force excessive per-reference voting round-trips, increasing the chance of a transaction taking so long it times out or ties up the transaction manager, degrading availability of the repository for other users.

This is a legitimate-flow DoS driven purely by attacker-controlled request shape (number of refs pushed), requiring no privileged access — any user capable of writing to a repository can trigger it.

### Likelihood Explanation
Likelihood is high for repositories that allow arbitrary ref creation by ordinary contributors (e.g., permissive branch/tag creation, CI-created refs, or personal namespaces): a single `git push` with many small ref updates is trivial to construct and requires no special tooling, mirroring how the reported vulnerability required only ordinary "create many small bids/asks" actions.

### Recommendation
Introduce an explicit, configurable cap on the number of reference updates processed within a single push/receive-pack or `UpdateReferences`/`UpdateReference`-with-hooks transaction, rejecting (with a clear `ResourceExhausted`/`InvalidArgument` error) pushes that exceed it, similar to the existing `max_queue_size`/`max_per_repo` concurrency controls and the per-RPC result limits already used elsewhere (`ForEachRef` count, `ListBlobs` limits, `UpdateRemoteMirror`'s `maxDivergentRefs`). Additionally, consider bounding per-push hook-invocation cost (e.g., batching the `update` hook call instead of one gRPC/process round trip per reference) to reduce the fan-out factor even for legitimately large pushes.

### Proof of Concept
Not directly executable from the indexed context (no test harness reproduction was run here), but the mechanism is demonstrable conceptually:
1. As an ordinary user with push access, construct a single `git push` (or `ReceivePack`/`SSHReceivePack`/`UserUpdateBranch`-style RPC sequence) containing N ref-update commands (e.g., N=50,000 new branch creations), each satisfying minimal Git validity requirements.
2. Send the push; Gitaly's receive-pack hook processing (`receivePackReferenceUpdates`) invokes `hookManager.UpdateHook` once per reference ( [1](#0-0) ), and, per `doc/hooks.md`'s execution-path diagram, each reference additionally triggers a `reference-transaction` prepare/commit round trip.
3. Observe that CPU/time cost scales linearly (with meaningful constant overhead per reference from process spawn + gRPC + potential Praefect voting), all inside a single RPC call that concurrency limiting treats as one unit of work — enabling repeated submission of such pushes to degrade the target repository's/node's availability for other users.

### Citations

**File:** internal/gitaly/hook/receivepack/receive_pack.go (L228-244)
```go
	for _, update := range updates {
		if err := hookManager.UpdateHook(
			ctx,
			req.GetRepository(),
			update.Ref.String(),
			update.OldOID.String(),
			update.NewOID.String(),
			[]string{hooksPayload},
			stdout, stderr,
		); err != nil {
			return fmt.Errorf("running update hook: %w", err)
		}

		if err := updater.Update(update.Ref, update.NewOID, update.OldOID); err != nil {
			return fmt.Errorf("queueing ref to be updated: %w", err)
		}
	}
```

**File:** doc/hooks.md (L266-287)
```markdown
    loop For each reference
        git-receive-pack->>+gitaly-hooks: exec "update"
        gitaly-hooks->>+Hook Service: call Update
        Hook Service->>+Custom Hooks: exec "update"
        Custom Hooks->>-Hook Service: OK
        Hook Service->>-gitaly-hooks: OK
        gitaly-hooks->>-git-receive-pack: OK

        git-receive-pack->>+gitaly-hooks: exec "reference-transaction prepare"
        gitaly-hooks->>+Hook Service: call ReferenceTransactionHook(prepare)
        Hook Service->>+Praefect: cast vote
        Praefect->>-Hook Service: OK
        Hook Service->>-gitaly-hooks: OK
        gitaly-hooks->>-git-receive-pack: OK

        git-receive-pack->>+gitaly-hooks: exec "reference-transaction commit"
        gitaly-hooks->>+Hook Service: call ReferenceTransaction(commit)
        Hook Service->>+Praefect: cast vote
        Praefect->>-Hook Service: OK
        Hook Service->>-gitaly-hooks: OK
        gitaly-hooks->>-git-receive-pack: OK
    end
```

**File:** internal/gitaly/hook/updateref/update_with_hooks.go (L244-248)
```go
	}

	if err := u.hookManager.UpdateHook(ctx, quarantinedRepo, reference.String(), oldrev.String(), newrev.String(), []string{hooksPayload}, &stdout, &stderr); err != nil {
		return fmt.Errorf("running update hooks: %w", wrapHookError(err, gitcmd.UpdateHook, stdout.String(), stderr.String()))
	}
```

**File:** internal/git/gitpipe/revision.go (L565-570)
```go
// WithCount is an option for ForEachRef to limit the number of results
func WithCount(count int) ForEachRefOption {
	return func(cfg *forEachRefConfig) {
		cfg.count = count
	}
}
```

**File:** internal/gitaly/service/blob/blobs.go (L113-130)
```go
		var i uint32
		for catfileInfoIter.Next() {
			blob := catfileInfoIter.Result()

			if err := callback(
				blob.ObjectID().String(),
				blob.ObjectSize(),
				nil,
				blob.ObjectName,
			); err != nil {
				return structerr.NewInternal("sending blob chunk: %w", err)
			}

			i++
			if blobsLimit > 0 && i >= blobsLimit {
				break
			}
		}
```

**File:** internal/gitaly/service/remote/update_remote_mirror_test.go (L500-507)
```go
			desc: "limits the number of divergent refs returned",
			sourceRefs: func() refs {
				out := refs{}
				for i := 0; i < maxDivergentRefs+1; i++ {
					out[fmt.Sprintf("refs/heads/branch-%03d", i)] = []string{"commit 1"}
				}
				return out
			}(),
```

**File:** doc/backpressure.md (L13-24)
```markdown
We employ concurrency limiting as our primary backpressure mechanism in Gitaly.

## Concurrency queue

Limit the number of concurrent RPCs that are in flight on each Gitaly node for each
repository per RPC using `[[concurrency]]` configuration:

```toml
[[concurrency]]
rpc = "/gitaly.SmartHTTPService/PostUploadPackWithSidechannel"
max_per_repo = 1
```
```
