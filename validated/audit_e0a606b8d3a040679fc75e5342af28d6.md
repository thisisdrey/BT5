### Title
Unbounded per-reference external hook/RPC invocations during non-atomic proc-receive processing enable resource exhaustion - (File: internal/gitaly/hook/receivepack/receive_pack.go)

### Summary
The DyDx report flags an unbounded loop that performs an external call once per iteration, where the iteration count is derived from an external/attacker-influenced quantity, creating a resource-exhaustion/DoS risk. The closest reachable analog in Gitaly is `procReceiveHook` in `internal/gitaly/hook/receivepack/receive_pack.go`, which iterates over the reference updates supplied by a push and, for the non-atomic case, invokes `receivePackReferenceUpdates` once per reference. Each of those invocations spawns a custom "update" hook process and drives a `git-update-ref` transaction (which itself triggers the reference-transaction hook and a Praefect vote round-trip per reference, as documented in `doc/hooks.md`). The number of loop iterations is fully controlled by the pushing client (number of ref updates in the push), and no cap on this count was found in the code.

### Finding Description
`procReceiveHook` branches on whether the proc-receive handler is atomic: [1](#0-0) 
In the non-atomic branch, it loops `for _, update := range handler.ReferenceUpdates()`, calling `receivePackReferenceUpdates` with a single-element slice for every reference update present in the push.

`receivePackReferenceUpdates` itself contains an inner loop over the supplied updates that, for each one, invokes `hookManager.UpdateHook` (spawning/executing the custom "update" hook, potentially a subprocess) and then queues the reference with `updater.Update`: [2](#0-1) 

Because the outer loop already isolates each reference into its own single-update call in the non-atomic path, this means the update hook is invoked, and a full reference-transaction/commit sequence performed, once per reference update in the push — with no visible upper bound on the number of references a single push (or proc-receive request) may contain.

This matches the documented flow in `doc/hooks.md`, which explicitly describes a `loop For each reference` where `gitaly-hooks` execs the `update` hook and then performs a `reference-transaction prepare`/`commit` round-trip to Praefect to cast a vote: [3](#0-2) 

Each iteration therefore performs at least one external process execution (the custom update hook, which may itself call out to arbitrary administrator-installed scripts) and, in a Praefect-backed cluster, a network RPC vote round trip — both of which are "external calls" analogous to the DyDx `SoloMargin` calls, and both scale linearly and unboundedly with a value (`handler.ReferenceUpdates()` length) that originates from an ordinary user's push request.

### Impact Explanation
An attacker with push access can submit a proc-receive request or push containing an extremely large number of distinct reference updates. Because the loop bound is the number of updates supplied, and there is no cap found anywhere in the reachable code path, this can force Gitaly to spawn a large number of hook subprocesses and, in HA/Praefect deployments, generate a correspondingly large number of transaction-vote RPC round trips sequentially. This can exhaust process-spawn resources, file descriptors, CPU, and cause the RPC handler to hold locks/resources for a long duration, degrading or denying service to that Gitaly node for the affected repository and consuming cluster-wide transaction/voting resources.

### Likelihood Explanation
Likelihood is moderate: reaching this path requires push access to a repository combined with use of the proc-receive protocol extension (`handler.ReferenceUpdates()`/non-atomic branch), which is a standard, unprivileged Git client capability (`git push` with many refspecs, or an SSH/HTTP push containing many ref updates). No special privilege beyond ordinary write/push access is required to increase the reference-update count arbitrarily.

### Recommendation
Impose an explicit, configurable upper bound on the number of reference updates processed per proc-receive/receive-pack invocation before dispatching per-reference hook executions and transaction votes, rejecting or chunking requests that exceed the limit. Consider batching update-hook invocations and reference-transaction votes instead of doing one full external-call round trip per reference when running in non-atomic mode.

### Proof of Concept
Conceptual PoC (not fully verified against a running Praefect cluster due to index limitations):
1. As a user with push access to a repository, craft a push (or interact with the proc-receive protocol directly) containing a very large number of distinct branch reference updates (e.g., tens of thousands of new lightweight refs) within a single push/session.
2. Observe that `procReceiveHook`'s non-atomic branch invokes `receivePackReferenceUpdates` once per reference update [4](#0-3) , each of which execs the "update" hook and performs a full reference-transaction commit cycle [5](#0-4) .
3. In a Praefect-managed cluster, each of these per-reference cycles requires a vote round trip as documented, multiplying network and CPU cost linearly with the number of refs supplied, with no observed cap in the reachable code.

Note: I was unable to fully verify whether the `handler.ReferenceUpdates()` value is size-limited elsewhere (e.g., by gRPC message size limits or an unexamined validation layer) due to the codebase index's coverage limits; a background Devin session with full repo access would be needed to confirm the absence of any such cap along the entire request path.

### Citations

**File:** internal/gitaly/hook/receivepack/receive_pack.go (L112-138)
```go
	} else {
		// Non-atomic reference updates are performed one at a time. Errors due to an update hook
		// failing are expected and should signal to the client it was rejected instead of
		// completely failing.
		for _, update := range handler.ReferenceUpdates() {
			if err := receivePackReferenceUpdates(
				ctx, cfg, req, repo, hookManager, []hook.ReferenceUpdate{update}, handler, handler,
			); err != nil {
				var (
					reason    string
					hookErr   hook.CustomHookError
					updateErr updateError
				)
				switch {
				case errors.As(err, &hookErr):
					reason = "update hook failed"
				case errors.As(err, &updateErr):
					reason = updateErr.Error()
				default:
					return fmt.Errorf("updating reference: %w", err)
				}

				rejectedUpdates[update.Ref] = reason
			} else {
				acceptedUpdates = append(acceptedUpdates, update)
			}
		}
```

**File:** internal/gitaly/hook/receivepack/receive_pack.go (L228-248)
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

	if err := updater.Commit(); err != nil {
		return updateError{error: err}
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
