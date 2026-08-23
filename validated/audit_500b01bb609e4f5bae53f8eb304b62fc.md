### Title
`postReceivePack` has no negotiation/idle timeout on the attacker-controlled pack stream, allowing indefinite handler/process pinning - (File: `internal/gitaly/service/smarthttp/receive_pack.go`)

### Summary
`postReceivePack` reads client-supplied stdin directly into `git-receive-pack --stateless-rpc` via a `streamio.NewReader` wrapping `stream.Recv()`, with no read/negotiation timeout applied to the pktline stream. Unlike the SSH `upload-pack`/`upload-archive` path, which wraps stdin in `pktline.NewReadMonitor` and enforces a timeout via `runUploadCommand`/`helper.Ticker` waiting for a specific boundary packet, `receive_pack.go` has no equivalent mechanism, so a client that simply never sends more data (and never sends flush) can keep the RPC, the underlying `git-receive-pack` process, and any associated locks/quarantine/hooks-state open indefinitely.

### Finding Description
In `postReceivePack` (`internal/gitaly/service/smarthttp/receive_pack.go:69-148`), the stdin for `git-receive-pack --stateless-rpc` is built directly from the gRPC stream: [1](#0-0) 
This reader blocks on `stream.Recv()` with no timeout, and `cmd.Wait()` blocks until `git-receive-pack` finishes reading stdin (EOF/flush) or errors: [2](#0-1) 

By contrast, the SSH `upload-pack`/`upload-archive` path explicitly guards against exactly this scenario: `runUploadCommand` wraps stdin with `pktline.NewReadMonitor`, which starts a goroutine watching for a boundary packet (e.g., the "done" packet) within a timeout window, and cancels the RPC's context if it's not observed: [3](#0-2) [4](#0-3) 

That comment explicitly documents the class of bug this defends against: "This timeout prevents a class of 'use-after-check' security issue when the access check for a git command is run before the command itself... if they can delay input for an arbitrarily long time, they can gain access days or weeks after the access check has completed."

`postReceivePack` has no analog of this mechanism. gRPC-level keepalive (`grpc.KeepaliveParams{Time: 5*time.Minute}` with default `Timeout`) only verifies transport liveness (TCP ping/pong) — it does not enforce forward progress of the application-level pktline stream. An attacker who is an authenticated, unprivileged GitLab user can push (or otherwise drive `PostReceivePack`) and, after sending the mandatory first `PostReceivePackRequest` (repository + `GlId`), simply stop sending further `Data` frames without closing the stream. Because the connection is kept alive at the transport layer (or the attacker responds to keepalive pings), `stream.Recv()` blocks forever, `git-receive-pack` blocks on stdin forever, and the goroutine/handler/subprocess/any repository lock state is pinned indefinitely — with no idle or negotiation timeout to bound it.

### Impact Explanation
This is a resource-exhaustion / DoS vector: each such stalled request consumes a Gitaly worker goroutine, a spawned `git-receive-pack` OS process, and potentially proc-receive hook/transaction state (`RegisterProcReceiveHook`) for as long as the attacker keeps the connection minimally alive. Repeating this concurrently (an unprivileged user can open many pushes/streams) can exhaust file descriptors, process table slots, and goroutines on a Gitaly node, degrading or denying service to other tenants — matching the GitLab bounty class "DoS / resource exhaustion or handler crash on the default configuration via a crafted pack, pktline stream, or negotiation input."

### Likelihood Explanation
No special privilege is required beyond the ability to push to a repository the attacker owns (a baseline capability of any GitLab user). The only requirement is holding a raw gRPC/HTTP stream open while withholding pktline data, which is straightforward to implement client-side and does not depend on git internals, malformed pack content, or any GitLab Rails behavior. It is reproducible on any default Gitaly deployment since the default gRPC keepalive settings do not bound idle application data.

### Recommendation
Apply the same negotiation/idle-timeout mitigation used for SSH `upload-pack`/`upload-archive` to `postReceivePack`: wrap the incoming stdin in a `pktline.NewReadMonitor`-style guard (or a simpler idle-read deadline) that cancels the RPC context if no forward progress (e.g., no flush-pkt, or no new data at all) is observed within a bounded window such as the existing `helper.Ticker`-based approach in `internal/gitaly/service/ssh/upload_command.go`. Ensure the derived context passed to `repo.Exec` for `receive-pack` is canceled on timeout so `cmd.Wait()` unblocks and the handler returns a `DeadlineExceeded` error rather than blocking indefinitely.

### Proof of Concept
```go
func TestPostReceivePack_StalledClientNeverSendsFlush(t *testing.T) {
    cfg := testcfg.Build(t)
    testcfg.BuildGitalyHooks(t, cfg)
    client, socketPath := runSmartHTTPServer(t, cfg)
    cfg.SocketPath = socketPath

    repo, _ := gittest.CreateRepository(t, testhelper.Context(t), cfg)

    ctx, cancel := context.WithTimeout(testhelper.Context(t), 30*time.Second)
    defer cancel()

    stream, err := client.PostReceivePack(ctx)
    require.NoError(t, err)

    require.NoError(t, stream.Send(&gitalypb.PostReceivePackRequest{
        Repository: repo,
        GlId:       "user-1",
    }))
    // Attacker never sends pack data, never sends flush, never closes send side.
    // Expectation absent a fix: stream.Recv() below blocks until the outer
    // test context (30s) expires, demonstrating no server-side idle/negotiation
    // timeout bounds the handler. With the recommended fix, the server itself
    // should return DeadlineExceeded well before the client-imposed timeout.
    _, err = stream.Recv()
    require.Error(t, err) // currently only bounded by the *client's* ctx timeout,
                           // not by any server-enforced negotiation/idle timeout
}
```
Note: this PoC demonstrates the *absence* of a server-side bound (the RPC only terminates because the test's client context times out); a server-side fix should make the RPC fail with a Gitaly-originated `DeadlineExceeded` well before that, analogous to the existing `testUploadPackWithSidechannelTimeout` test in `internal/gitaly/service/ssh/upload_pack_test.go` for the SSH path.

### Citations

**File:** internal/gitaly/service/smarthttp/receive_pack.go (L75-78)
```go
	stdin := streamio.NewReader(func() ([]byte, error) {
		resp, err := stream.Recv()
		return resp.GetData(), err
	})
```

**File:** internal/gitaly/service/smarthttp/receive_pack.go (L143-145)
```go
	if err := cmd.Wait(); err != nil {
		return structerr.NewFailedPrecondition("waiting for receive-pack: %w", err)
	}
```

**File:** internal/gitaly/service/ssh/upload_command.go (L46-63)
```go
	stdinPipe, monitor, cleanup, err := pktline.NewReadMonitor(ctx, stdin, s.logger)
	if err != nil {
		return fmt.Errorf("create monitor: %w", err)
	}

	cmd, err := repo.Exec(ctx, sc, append([]gitcmd.CmdOpt{
		gitcmd.WithStdin(stdinPipe),
		gitcmd.WithStdout(stdout),
		gitcmd.WithStderr(stderr),
		gitcmd.WithFinalizer(func(context.Context, *command.Command) { cleanup() }),
	}, opts...)...)
	stdinPipe.Close() // this now belongs to cmd
	if err != nil {
		cleanup()
		return fmt.Errorf("starting command: %w", err)
	}

	go monitor.Monitor(ctx, boundaryPacket, timeoutTicker, cancelCtx)
```

**File:** internal/git/pktline/read_monitor.go (L14-30)
```go
// ReadMonitor monitors an io.Reader, waiting for a specified packet. If the
// packet doesn't come within a timeout, a cancel function is called. This can
// be used to place a timeout on the *negotiation* phase of some git commands,
// aborting them if it is exceeded.
//
// This timeout prevents a class of "use-after-check" security issue when the
// access check for a git command is run before the command itself. The user
// has control of stdin for the git command, and if they can delay input for
// an arbitrarily long time, they can gain access days or weeks after the
// access check has completed.
//
// This approach is better than placing a timeout on the overall git operation
// because there is a conflict between mitigating the use-after-check with a
// short timeout, and allowing long-lived git operations to complete. The
// negotiation phase is a small proportion of the time taken for a large git
// fetch, for instance, so tighter limits can be placed on it, leading to a
// better mitigation.
```
