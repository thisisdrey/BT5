### Title
Cross-Transaction Reference-Update Injection / DoS via directly-callable `ProcReceiveHook` RPC - ([File: internal/gitaly/service/hook/proc_receive.go])

### Summary
The `HookService.ProcReceiveHook` RPC is meant to be invoked only indirectly by the `proc-receive` Git hook executable that Gitaly itself spawns during a `ReceivePack` operation. In practice it is a normal, externally-callable gRPC handler that is looked up and matched to a pending server-side operation purely by an attacker-suppliable `TransactionID`, with no verification that the caller is the actual hook process spawned for that specific transaction.

### Finding Description
`RegisterProcReceiveHook` (`internal/gitaly/hook/receivepack/receive_pack.go:26-73`) registers a "waiter" in the `ProcReceiveRegistry` keyed only by `storage.TransactionID`, and then blocks waiting for a handler on that channel: [1](#0-0) 

The `ProcReceiveHook` gRPC handler builds a `ProcReceiveHandler` purely from the `TransactionID` embedded in the caller's supplied environment variables (`HooksPayload`) and then calls `registry.Transmit`, which looks the waiter up **only by that numeric ID** and hands the handler — including attacker-controlled `ReferenceUpdates()` parsed from the request's pkt-line stdin — straight to whichever goroutine is waiting on it: [2](#0-1) [3](#0-2) 

Once matched, `procReceiveHook()`/`receivePackReferenceUpdates()` performs actual `git-update-ref` writes into the **victim's repository/transaction** using the reference names, old OIDs, and new OIDs taken from the attacker's fabricated pkt-line payload (`internal/gitaly/hook/procreceive_handler.go:80-98`, consumed at `internal/gitaly/hook/receivepack/receive_pack.go:100-138,200-230`).

Nothing in this path validates that the entity calling `ProcReceiveHook` is the specific `proc-receive` process Gitaly spawned for that `TransactionID` — it is solely a numeric key lookup, analogous to the reported Solidity bug where `executeTransaction()` performed the sensitive queued-check/state-transition using only a `txHash` argument with no restriction that the caller be the contract's own internal `execute()` flow. The authentication on the internal gRPC socket over which `ProcReceiveHook` is exposed uses `cfg.Auth.GetToken()` (`internal/git/gitcmd/hooks_payload.go:171`), which is the **same shared secret** issued to every hook invocation and to ordinary Gitaly clients, not a per-transaction/per-caller credential — so any actor able to trigger a push (and thus obtain this token via their own hook's environment) can call the RPC directly for an unrelated `TransactionID`.

### Impact Explanation
An attacker who can trigger any push against a Gitaly instance obtains the shared internal socket token, and, if they can guess or observe a concurrently in-flight `TransactionID` (these appear to be small, likely sequential, values assigned per-transaction — see `storage.TransactionID` usage across `internal/gitaly/hook/receivepack/receive_pack.go` and `internal/gitaly/service/hook/proc_receive_test.go`), can call `ProcReceiveHook` directly with that ID before the legitimate `proc-receive` process does. This:
- Races and can preempt the legitimate hook's own `Transmit` call for the same ID, causing the legitimate victim push's proc-receive coordination to fail entirely (denial of service on that push, mirroring the reported "stuck proposal" DoS).
- Worse, since the injected `ReferenceUpdates()` are attacker-controlled while the actual `git-update-ref` write happens against the victim's repository/transaction, this permits injecting arbitrary reference name/old-OID/new-OID triples into someone else's push, a cross-transaction/cross-repository reference-update forgery, not merely a DoS.

### Likelihood Explanation
Likelihood depends on: (1) an attacker being able to obtain the shared `InternalSocketToken` (achievable by triggering any of their own hook invocations, since the token is the standard shared Gitaly auth secret, not a session-unique credential), and (2) predicting/enumerating a victim's in-flight `TransactionID` and winning a race against the real proc-receive hook process. Because IDs appear to be simple sequential counters rather than unguessable tokens, and the window between `RegisterProcReceiveHook`'s registration and the real hook's `Transmit` call is a normal git subprocess spawn delay, this is plausibly exploitable, though the exact ID-generation scheme was not fully confirmed in the code reviewed.

### Recommendation
Bind the `ProcReceiveHook` RPC (and `ProcReceiveRegistry.Transmit`) to more than an attacker-suppliable transaction ID: require and verify a per-invocation, unguessable, single-use secret (minted when `RegisterProcReceiveHook` registers the waiter and passed only to the specific spawned hook process's environment), so that `Transmit` cannot be satisfied by an unrelated caller even if they know or guess the transaction ID.

### Proof of Concept
1. As an ordinary user, initiate any push (e.g., `git push`) against the target Gitaly host to obtain a valid `InternalSocketToken` from your own hook's environment (`GITALY_HOOKS_PAYLOAD`).
2. Concurrently, observe or predict the `TransactionID` assigned to a victim's in-flight push (e.g., by rapidly enumerating small integers or racing your own push against theirs on the same storage).
3. Using the obtained token, connect directly to Gitaly's internal socket and call `HookService.ProcReceiveHook`, sending a crafted `HooksPayload` environment variable with the victim's guessed `TransactionID` and a fabricated pkt-line stream (`version=1\0atomic`, followed by a forged `<old-oid> <new-oid> <ref>` line and flush), as constructed in the test helper at `internal/gitaly/service/hook/proc_receive_test.go:32-76`.
4. If this call reaches `registry.Transmit` before the legitimate `proc-receive` process does, the victim's `RegisterProcReceiveHook` goroutine will consume the attacker's forged handler and either fail the victim's push (DoS) or apply the attacker's forged reference update through `receivePackReferenceUpdates` against the victim's repository.

### Citations

**File:** internal/gitaly/hook/receivepack/receive_pack.go (L39-60)
```go
	tx, err := txRegistry.Get(transactionID)
	if err != nil {
		return nil, fmt.Errorf("getting transaction: %w", err)
	}

	registry := hookManager.ProcReceiveRegistry()
	handlerCh, cleanup, err := registry.RegisterWaiter(transactionID)
	if err != nil {
		return nil, fmt.Errorf("registering waiter: %w", err)
	}

	go func() {
		select {
		case <-ctx.Done():
		case <-receiveDoneCh:
		case handler := <-handlerCh:
			if err := procReceiveHook(ctx, logger, cfg, req, repo, hookManager, tx, handler); err != nil {
				handlerErrCh <- err
			}
		}
		close(handlerErrCh)
	}()
```

**File:** internal/gitaly/service/hook/proc_receive.go (L24-56)
```go
func (s *server) ProcReceiveHook(stream gitalypb.HookService_ProcReceiveHookServer) error {
	ctx := stream.Context()

	firstRequest, err := stream.Recv()
	if err != nil {
		return structerr.NewInternal("receiving first request: %w", err)
	}

	stdin := streamio.NewReader(func() ([]byte, error) {
		req, err := stream.Recv()
		return req.GetStdin(), err
	})

	var m sync.Mutex
	stdout := streamio.NewSyncWriter(&m, func(p []byte) error {
		return stream.Send(&gitalypb.ProcReceiveHookResponse{Stdout: p})
	})

	stderr := streamio.NewSyncWriter(&m, func(p []byte) error {
		return stream.Send(&gitalypb.ProcReceiveHookResponse{Stderr: p})
	})

	handler, doneCh, err := gitalyhook.NewProcReceiveHandler(
		firstRequest.GetEnvironmentVariables(), stdin, stdout, stderr,
	)
	if err != nil {
		return structerr.NewInternal("creating handler: %w", err)
	}

	registry := s.manager.ProcReceiveRegistry()
	if err := registry.Transmit(ctx, handler); err != nil {
		return sendProcReceiveHookResponse(stream, 1, fmt.Sprintf("transmitting handler: %s", err))
	}
```

**File:** internal/gitaly/hook/procreceive_registry.go (L101-119)
```go
// Transmit transmits a handler to its waiter.
func (r *ProcReceiveRegistry) Transmit(ctx context.Context, handler ProcReceiveHandler) error {
	r.m.Lock()
	ch, ok := r.waiters[handler.TransactionID()]
	r.m.Unlock()

	if !ok {
		return fmt.Errorf("no waiters for id: %d", handler.TransactionID())
	}

	// It is possible that the RPC (waiter) returned because receive-pack
	// returned an error. In such scenarios, we don't want to block indefinitely.
	select {
	case <-ctx.Done():
		return ctx.Err()
	case ch <- handler:
	}

	return nil
```
