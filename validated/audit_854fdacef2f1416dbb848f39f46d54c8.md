### Title
Unbounded per-reference serial loop in non-atomic proc-receive hook allows push-triggered RPC handler resource exhaustion / DoS - (File: internal/gitaly/hook/receivepack/receive_pack.go)

### Summary
`SSHReceivePack`/`PostReceivePack` requests go through `RegisterProcReceiveHook` → `procReceiveHook`, which, for non-atomic proc-receive handlers, iterates once per reference update contained in a single push and, for each one, spawns a full `updateref.New` process, invokes the custom `update` hook via `hookManager.UpdateHook` (a synchronous round-trip, potentially to the GitLab internal API), and commits the ref transaction — all serially, with no upper bound on the number of iterations.

### Finding Description
In `procReceiveHook`, the non-atomic branch performs: [1](#0-0) 
For each reference update, `receivePackReferenceUpdates` is invoked, which spawns a new `git update-ref` subprocess and runs the `update` hook for every single ref before committing: [2](#0-1) 

The number of loop iterations equals `len(handler.ReferenceUpdates())`, which is directly controlled by the pushing client: an ordinary user can construct a single `git push` (or SSH/HTTP receive-pack request) that updates an arbitrarily large number of references (branches/tags) in one request. Each iteration does non-trivial, non-batched work: spawning a git process (`updateref.New`), and a full hook round-trip (`hookManager.UpdateHook`, which in GitLab's deployment calls back into the internal Rails API over HTTP). Unlike the atomic path — which performs a single bulk `receivePackReferenceUpdates` call for all updates — the non-atomic path deliberately processes updates one-by-one to allow partial acceptance/rejection, but does so without any cap on the number of updates processed per RPC invocation.

This directly mirrors the reported bug class: a loop whose iteration count scales with an attacker/user-controlled quantity (number of deposits / here, number of pushed ref updates) with no `maxLoops`-style bound, causing the handler's resource consumption (CPU, subprocess spawns, and hook RPC latency) to grow unbounded with input size until the RPC times out or the connection/resources are exhausted.

### Impact Explanation
A client pushing a very large number of ref updates in one non-atomic proc-receive push forces Gitaly to serially spawn a process and perform a hook round-trip per reference, with no batching or limit. This can exhaust the RPC's time budget (context deadline) or Gitaly's process/goroutine resources, causing the RPC to fail or the connection to be held open excessively — a functional availability/DoS impact on that RPC handler, without requiring any privileged access. Because the transaction is only committed after all per-ref hook invocations succeed or are recorded as rejected, an oversized push can also leave a large in-flight transaction consuming resources for the duration of the loop.

### Likelihood Explanation
Any authenticated but otherwise unprivileged Git user capable of pushing to a repository that uses proc-receive (non-atomic mode) can trigger this by including a large number of ref updates (e.g., pushing/creating a large number of new branches or tags) in one push. No malicious peer, MITM, or token leak is required — it is triggerable through the standard push path with a crafted (but valid) receive-pack request.

### Recommendation
Introduce a configurable maximum number of reference updates processed per non-atomic proc-receive invocation (mirroring the `maxLoops`/cap approach recommended in the referenced report), rejecting or chunking pushes that exceed the limit, and/or apply the existing concurrency/queue-based backpressure model (as used for e.g. `pack_objects_limiting`) to bound the aggregate cost of per-reference hook invocations within a single RPC.

### Proof of Concept
1. As a normal user with push access to a repository configured with a non-atomic proc-receive hook, run `git push` with a large number of ref updates in a single invocation, e.g.:
   ```
   for i in $(seq 1 200000); do git branch "b$i" HEAD; done
   git push origin --all
   ```
2. Gitaly's `procReceiveHook` enters the non-atomic branch and executes `receivePackReferenceUpdates` 200,000 times in serial `for _, update := range handler.ReferenceUpdates()`, each spawning `git update-ref` and invoking the `update` hook (potentially a Rails API HTTP call) before the loop can complete.
3. With enough ref updates, the RPC exceeds its deadline or consumes disproportionate CPU/process resources, denying availability of the receive-pack RPC for that repository, matching the reported "loop scales with user-controlled input, no cap" bug class.

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

**File:** internal/gitaly/hook/receivepack/receive_pack.go (L200-244)
```go
func receivePackReferenceUpdates(
	ctx context.Context,
	cfg config.Cfg,
	req gitcmd.ReceivePackRequest,
	repo *localrepo.Repo,
	hookManager hook.Manager,
	updates []hook.ReferenceUpdate,
	stdout, stderr io.Writer,
) (returnedErr error) {
	hooksPayload, err := setupHooksPayloadEnv(ctx, cfg, req, repo, gitcmd.UpdateHook)
	if err != nil {
		return fmt.Errorf("creating hooks payload: %w", err)
	}

	updater, err := updateref.New(ctx, repo, updateref.WithNoDeref())
	if err != nil {
		return fmt.Errorf("spawning ref updater: %w", err)
	}
	defer func() {
		if err := updater.Close(); err != nil && returnedErr == nil {
			returnedErr = fmt.Errorf("cancel ref updater: %w", err)
		}
	}()

	if err := updater.Start(); err != nil {
		return fmt.Errorf("start reference transaction: %w", err)
	}

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
