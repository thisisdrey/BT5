### Title
Unbounded per-reference custom-hook execution loop in proc-receive push handling enables push-triggered resource-exhaustion DoS - ([File: internal/gitaly/hook/receivepack/receive_pack.go])

### Summary
The proc-receive push-handling path in Gitaly parses an attacker/client-controlled, unbounded list of reference updates from the push wire protocol and then synchronously loops over every single update to spawn a custom hook process, with no cap on the number of updates or aggregate work performed in one RPC/transaction — the same "unbounded gas/gas-bomb" bug class described in the external report (an operation whose cost scales linearly, without limit, with attacker-supplied cardinality of a stored/streamed collection).

### Finding Description
When a client pushes with the `proc-receive` feature, `NewProcReceiveHandler` reads reference updates off the pkt-line stream in an unbounded loop and appends them into an in-memory slice with no size limit: [1](#0-0) 

That handler is subsequently used by `procReceiveHook`, which — for the non-atomic case — iterates over every single update returned by `handler.ReferenceUpdates()` and, for each one, calls `receivePackReferenceUpdates`: [2](#0-1) 

Inside `receivePackReferenceUpdates`, each individual reference update triggers a synchronous, expensive `hookManager.UpdateHook` invocation (which spawns a custom hook subprocess) before being queued into the reference updater: [3](#0-2) 

Unlike gRPC-message-based mutators (e.g. `DeleteRefs`, `UpdateReferences`), which are ultimately bounded by gRPC's max message size or by proto-level streaming batching, the number of reference updates processed here comes from Git's native pkt-line push protocol carried over the SSH/HTTP receive-pack RPCs (`internal/gitaly/service/ssh/receive_pack.go`, `internal/gitaly/service/smarthttp/receive_pack.go`), which does not impose the same per-message size cap and is driven entirely by how many refs the pushing client's Git client chooses to update in a single push. This lets an ordinary, otherwise-authorized pusher force Gitaly to perform O(n) subprocess spawns and reference-transaction queuing operations within a single RPC call/transaction, with `n` unbounded by Gitaly itself.

### Impact Explanation
A push with a very large number of reference updates (creations/deletions/updates of many branches or tags in one push) causes Gitaly to synchronously spawn a custom hook subprocess for every single ref before committing the reference transaction. In the worst case this can consume enough CPU/process/time budget to exceed RPC or client timeouts, causing the push (and its underlying transaction/lock) to fail repeatedly, and — depending on how the transaction and any held locks are cleaned up on failure/timeout — can degrade or deny availability of the affected repository for further pushes, mirroring the "operation with unbounded cost preventing successful completion" impact of the reported `onUninstall()` issue.

### Likelihood Explanation
Likelihood is moderate-to-high in environments where proc-receive is enabled: the primitive is reachable directly from an ordinary user's `git push`, requires no special privilege beyond normal push access, and Git natively allows a single push to touch a very large number of refs. The main mitigating factor is that other layers (e.g. Git's own pack negotiation, custom hook execution time, GitLab's own push limits) may reduce practical exploitability, but nothing in the reviewed Gitaly code itself caps the number of proc-receive updates processed per RPC.

### Recommendation
Impose an explicit upper bound on the number of reference updates accepted/processed per proc-receive invocation (and reject or chunk pushes that exceed it), and/or bound the aggregate hook-execution time for the update-hook loop in `receivePackReferenceUpdates`, so a single push cannot force unbounded synchronous subprocess-spawning work within one transaction.

### Proof of Concept
1. Enable the proc-receive hook path for a repository/transaction-managed setup.
2. From a normal, authorized client, perform a single `git push` (SSH or HTTP) with the `proc-receive` capability that specifies an extremely large number of reference updates (e.g. thousands of new/deleted lightweight branches) in one push.
3. Observe that `NewProcReceiveHandler` accepts an unbounded number of updates [1](#0-0)  and `procReceiveHook`/`receivePackReferenceUpdates` synchronously spawn one custom-hook subprocess per update [3](#0-2) , causing the RPC's processing time/resource cost to scale linearly and unboundedly with the number of refs pushed, with no server-side cap.

### Citations

**File:** internal/gitaly/hook/procreceive_handler.go (L80-99)
```go
	var updates []ReferenceUpdate
	for scanner.Scan() {
		line := scanner.Bytes()

		// When all reference updates are transmitted, we expect a flush.
		if pktline.IsFlush(line) {
			break
		}

		data, err := pktline.Payload(line)
		if err != nil {
			return nil, nil, fmt.Errorf("receiving reference update: %w", err)
		}

		update, err := parseRefUpdate(data)
		if err != nil {
			return nil, nil, fmt.Errorf("parse reference update: %w", err)
		}
		updates = append(updates, update)
	}
```

**File:** internal/gitaly/hook/receivepack/receive_pack.go (L112-139)
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
	}
```

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
