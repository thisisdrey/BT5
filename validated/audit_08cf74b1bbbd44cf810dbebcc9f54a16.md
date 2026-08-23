### Title
Missing negotiation/idle timeout on `ReceivePack` allows a stalled client stream to hold the RPC handler and `git-receive-pack` subprocess open indefinitely - ([File: internal/gitaly/service/smarthttp/receive_pack.go])

### Summary
`PostReceivePack`/`postReceivePack` (and the analogous `SSHReceivePack`) feed client-controlled stream data directly into `git-receive-pack --stateless-rpc` via a blocking `streamio.NewReader`/`stream.Recv()` loop with no per-phase timeout, unlike `upload-pack`, which is explicitly protected by a `pktline.ReadMonitor` + `timeoutTicker` (`internal/gitaly/service/ssh/upload_command.go`). A client that opens the stream, sends the required first request, and then never sends further pktlines/flush can keep the handler and `git-receive-pack` process blocked on stdin for as long as the RPC deadline (or connection) allows.

### Finding Description
`postReceivePack` in `internal/gitaly/service/smarthttp/receive_pack.go:69-148` wires `stdin := streamio.NewReader(func() ([]byte, error) { resp, err := stream.Recv(); return resp.GetData(), err })` directly to `gitcmd.WithStdin(stdin)` for `git receive-pack --stateless-rpc`, then blocks on `cmd.Wait()` [1](#0-0) [2](#0-1) . There is no equivalent of the `pktline.ReadMonitor`/`timeoutTicker` mechanism used for `upload-pack`, which explicitly exists to "place a timeout on the negotiation phase of some git commands, aborting them if it is exceeded" to mitigate use-after-check-style attacks [3](#0-2) , driven by `runUploadCommand`'s `monitor.Monitor(ctx, boundaryPacket, timeoutTicker, cancelCtx)` [4](#0-3) . `ReceivePack`/`PostReceivePack` has no such construct: if the attacker (an authenticated, unprivileged user who can push to their own project) opens the stream, sends the mandatory first message (with valid `GlId`/`Repository`), and then simply never sends the pktline command list or any flush packet, `stream.Recv()` blocks, `git-receive-pack`'s stdin read blocks, and `cmd.Wait()` in `postReceivePack` blocks with it, holding open the gRPC handler goroutine, the spawned OS process, and any locks/quarantine directories associated with the push until the surrounding context is torn down. The SSH variant (`internal/gitaly/service/ssh/receive_pack.go`) has the exact same structural gap, and it even documents a related but different risk: an explicit comment notes that if `git-receive-pack` crashes before EOF, `cmd.Wait()` could block forever because of the stdin-copy goroutine, which the code works around with an `os.Pipe()`, but this workaround only addresses the "git process exits early" case, not the "client never sends anything" case [5](#0-4) .

The only bound on how long this can persist is the overall gRPC/HTTP request deadline set by the client itself (attacker-controlled) or general connection-level keepalive settings (`grpc.KeepaliveParams{Time: 5 * time.Minute}` in `internal/gitaly/server/server.go`), which govern idle *connections*, not idle *streams* with an open RPC — a stream that is actively read from (blocked in `Recv`) is not affected by those settings. gRPC's default behavior does not impose a server-side idle timeout on an open stream unless a context deadline is set.

### Impact Explanation
This matches the GitLab bounty class of "DoS / resource exhaustion or handler crash on the default configuration." An attacker holds open one `git-receive-pack` subprocess plus its associated gRPC stream, quarantine directory, and worker goroutine per stalled request, indefinitely (bounded only by client-chosen or absent deadlines). Repeating this many times in parallel from a single unprivileged, authenticated account (who can push to a repo they own) can exhaust the pool of concurrent receive-pack processes/goroutines/file descriptors on the Gitaly node, degrading service for other tenants sharing the storage/node. This is resource exhaustion, not remote code execution or cross-repository disclosure.

### Likelihood Explanation
Any authenticated GitLab user capable of pushing to a repository they own can open `PostReceivePack`/`SSHReceivePack`, send the valid first message, and then withhold the rest of the stream — no special role or configuration is required, and the attack is trivially repeatable and can be parallelized across many concurrent connections to amplify effect. The only mitigation is the caller's/gateway's own request timeout (e.g. Workhorse or gRPC client deadline), which is not enforced inside Gitaly's `ReceivePack` handler itself and is not guaranteed by default.

### Recommendation
Introduce a negotiation/idle read timeout for `ReceivePack` symmetric to the one already applied to `upload-pack`: wrap the incoming stdin reader with a `pktline.ReadMonitor`-style watchdog (or a simpler idle-read deadline) that cancels the RPC context if no data (or no flush/terminal packet) is observed within a bounded window after the RPC starts, in both `internal/gitaly/service/smarthttp/receive_pack.go` and `internal/gitaly/service/ssh/receive_pack.go`. Ensure `cmd.Wait()` is bounded by this cancellation so the subprocess and goroutines are reliably torn down.

### Proof of Concept
```go
// Illustrative: open PostReceivePack, send only the header request, then withhold
// any further data. cmd.Wait() in postReceivePack blocks until ctx/gRPC deadline,
// with no idle/negotiation timeout enforced by the handler itself.
stream, err := client.PostReceivePack(ctx)
require.NoError(t, err)

require.NoError(t, stream.Send(&gitalypb.PostReceivePackRequest{
    Repository: repoProto,
    GlId:       "user-123",
}))
// Do not send any further PostReceivePackRequest with Data, and do not CloseSend.
// Expected (if fixed): the RPC returns a DeadlineExceeded/Canceled error within a
// short, configurable negotiation window.
// Observed (vulnerable): the RPC and the git-receive-pack process remain blocked
// until the client's own context deadline (or none at all) is reached.
```

### Citations

**File:** internal/gitaly/service/smarthttp/receive_pack.go (L75-78)
```go
	stdin := streamio.NewReader(func() ([]byte, error) {
		resp, err := stream.Recv()
		return resp.GetData(), err
	})
```

**File:** internal/gitaly/service/smarthttp/receive_pack.go (L127-145)
```go
	cmd, err := repo.Exec(ctx,
		gitcmd.Command{
			Name:  "receive-pack",
			Flags: []gitcmd.Option{gitcmd.Flag{Name: "--stateless-rpc"}},
			Args:  []string{repoPath},
		},
		gitcmd.WithStdin(stdin),
		gitcmd.WithStdout(stdout),
		gitcmd.WithReceivePackHooks(objectHash, req, "http", transactionsEnabled),
		gitcmd.WithGitProtocol(s.logger, req),
		gitcmd.WithConfig(config...),
	)
	if err != nil {
		return structerr.NewFailedPrecondition("spawning receive-pack: %w", err)
	}

	if err := cmd.Wait(); err != nil {
		return structerr.NewFailedPrecondition("waiting for receive-pack: %w", err)
	}
```

**File:** internal/git/pktline/read_monitor.go (L14-23)
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

**File:** internal/gitaly/service/ssh/receive_pack.go (L100-117)
```go

	// When an `exec.Cmd` has its `cmd.Stdin` configured with an `io.Reader`
	// that is not also of type `os.File` a goroutine is automatically
	// configured that performs an `io.Copy()` between the reader and a newly
	// created pipe. A problem with this can arise when `cmd.Wait()` is invoked
	// because it waits not only for the process to complete but also all the
	// goroutine to end. If the configured `cmd.Stdin` is only of type
	// `io.Reader` and never closed, the goroutine will never end. This leads to
	// `cmd.Wait()` being blocked indefinitely.
	//
	// Within Gitaly this problem can manifest itself when a git process crashes
	// before `stdin` reaches EOF. To date this has only been noticed as a
	// problem for the `SSHReceivePack` RPC, so a pipe and goroutine have been
	// created explicitly to prevent `cmd.Wait()` from blocking indefinitely.
	pr, pw, err := os.Pipe()
	if err != nil {
		return fmt.Errorf("creating pipe: %w", err)
	}
```
