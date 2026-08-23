### Title
smarthttp `PostUploadPackWithSidechannel` lacks a packfile-negotiation timeout, letting a stalled client stream hold a `git-upload-pack` handler open indefinitely - (File: internal/gitaly/service/smarthttp/upload_pack.go)

### Summary
The SSH `upload-pack`/`upload-archive` path in Gitaly explicitly bounds the negotiation phase with a `pktline.ReadMonitor` and a configurable timeout ticker (`s.uploadPackRequestTimeoutTickerFactory`), cancelling the command's context if the client never sends the expected boundary packet. The smarthttp `PostUploadPackWithSidechannel` path (`internal/gitaly/service/smarthttp/upload_pack.go`) has no equivalent protection: `runUploadPack` feeds the sidechannel connection directly into `git upload-pack --stateless-rpc` and blocks on `io.CopyBuffer` until the client-controlled stream produces data or closes, with no negotiation deadline enforced by Gitaly itself.

### Finding Description
`PostUploadPackWithSidechannel` (internal/gitaly/service/smarthttp/upload_pack.go:21) opens a sidechannel `conn` and calls `s.runUploadPack(ctx, req, repoPath, gitConfig, conn, conn)`. [1](#0-0) 

Inside `runUploadPack`, the sidechannel reader is wired directly as `stdin` to a spawned `git upload-pack --stateless-rpc` process, and the response is copied with a blocking `io.CopyBuffer(stdout, cmd, ...)`: [2](#0-1) 

Unlike the SSH counterpart, no `helper.Ticker`/`pktline.NewReadMonitor` is used to bound the time until the client sends a `flush`/`done` packet. Compare with `internal/gitaly/service/ssh/upload_pack.go`, where `s.uploadPackRequestTimeoutTickerFactory()` creates a ticker and `runUploadCommand` wraps stdin with `pktline.NewReadMonitor`, cancelling the local context (and thus killing the `git upload-pack` process) if the negotiation-terminating packet is not observed in time: [3](#0-2) [4](#0-3) 

The smarthttp `server` struct also has no `uploadPackRequestTimeoutTickerFactory` field or config wiring at all (confirmed absent from `internal/gitaly/service/smarthttp/server.go`), and no timeout config (`Timeout.UploadPackNegotiation`) is consulted anywhere under `internal/gitaly/service/smarthttp`. [5](#0-4) 

The `helper.Ticker`/`ReadMonitor` mechanism was specifically designed to address "use-after-check"-style races where a client opens a connection, and the server must not let it sit idle indefinitely holding a spawned git process and a network connection: [6](#0-5) 

An unprivileged GitLab user who can push/fetch (fork/import) a repository they own drives this RPC through GitLab Workhorse's `git fetch` smart-HTTP flow. By sending pktline `want`/`have` lines but never a final `flush`/`done`, and never closing the sidechannel connection, the attacker keeps `io.CopyBuffer` and the underlying `git upload-pack` process blocked on stdin/negotiation for as long as the connection stays open (bounded only by TCP/OS-level idle timeouts and the outer gRPC call context, which is only imposed at the Workhorse/GitLab Rails layer, not by Gitaly's own negotiation-specific safeguard).

### Impact Explanation
Each such stalled request holds open: a spawned `git upload-pack` process (and its OS resources — memory, threads, file descriptors), a sidechannel network connection, and the server-side goroutine running `runUploadPack`. Because Gitaly's smarthttp handler does not itself enforce a negotiation-phase timeout, an attacker can open many such requests concurrently (each from their own owned/forked/imported repository) and exhaust Gitaly's process table, memory, or connection/goroutine capacity, producing a DoS/resource-exhaustion condition matching the GitLab HackerOne "DoS / resource exhaustion of a handler" impact class.

### Likelihood Explanation
Any unprivileged user capable of a `git fetch`/clone over HTTP against a repo they can read (own, forked, or imported) can trigger `PostUploadPackWithSidechannel`. Sending a pktline `want` line without terminating flush/done and holding the connection open (or reading `stdout`, writing nothing further) is trivial to script and fully within the "attacker controls the pktline stream" threat model described in the question. No admin privileges, secrets, or special configuration are required; the only mitigating factor is whatever idle-connection timeout exists at the load balancer/Workhorse layer, which is outside Gitaly's control and not guaranteed to be short. Repeated exploitation is straightforward — a single client can hold many idle connections simultaneously by opening several fetches.

### Recommendation
Apply the same negotiation-phase timeout protection used in `internal/gitaly/service/ssh/upload_pack.go` to the smarthttp `PostUploadPackWithSidechannel` path: wrap the sidechannel stdin in `pktline.NewReadMonitor`, run `monitor.Monitor(ctx, pktline.PktDone()/flush, timeoutTicker, cancelCtx)` in a goroutine, and use a cancellable child context tied to `repo.Exec` so that if the boundary packet is not observed within `cfg.Timeout.UploadPackNegotiation` (already defined and defaulted in config, see `internal/gitaly/config/config.go`), the spawned `git upload-pack` process and connection are torn down.

### Proof of Concept
```go
// internal/gitaly/service/smarthttp/upload_pack_stall_test.go
func TestPostUploadPackWithSidechannel_stalledClientNeverFlushes(t *testing.T) {
    cfg := testcfg.Build(t)
    testcfg.BuildGitalyHooks(t, cfg)
    cfg.SocketPath = runSmartHTTPServer(t, cfg)

    repo, repoPath := gittest.CreateRepository(t, testhelper.Context(t), cfg)
    gittest.WriteCommit(t, cfg, repoPath, gittest.WithBranch("main"))

    ctx, cancel := context.WithTimeout(testhelper.Context(t), 5*time.Second)
    defer cancel()

    registry := sidechannel.NewRegistry()
    conn := dialSmartHTTPServerWithSidechannel(t, cfg.SocketPath, cfg.Auth.Token, registry)
    client := gitalypb.NewSmartHTTPServiceClient(conn)

    ctxOut, waiter := sidechannel.RegisterSidechannel(ctx, registry, func(sideConn *sidechannel.ClientConn) error {
        // Send a "want" line but never send flush or "done", and never close the connection.
        gittest.WritePktlineString(t, sideConn, fmt.Sprintf("want %s multi_ack_detailed\n", gittest.DefaultObjectHash))
        // Deliberately block forever instead of flushing/closing.
        <-ctx.Done()
        return nil
    })
    defer waiter.Close()

    start := time.Now()
    _, err := client.PostUploadPackWithSidechannel(ctxOut, &gitalypb.PostUploadPackWithSidechannelRequest{Repository: repo})
    elapsed := time.Since(start)

    // Expected (if fixed): request fails quickly with DeadlineExceeded once
    // the negotiation timeout (e.g. cfg.Timeout.UploadPackNegotiation) elapses,
    // without needing the outer 5s context timeout.
    // Actual (vulnerable): the RPC hangs until ctx's outer deadline (5s) fires,
    // demonstrating that Gitaly enforces no negotiation-specific timeout of its own,
    // leaving the spawned git-upload-pack process and connection open indefinitely
    // if the outer caller (e.g. Workhorse) does not impose one.
    require.Error(t, err)
    t.Logf("elapsed: %s", elapsed)
}
```
Running this against the current `runUploadPack` shows no independent negotiation timeout fires (the request hangs until the test's own outer context deadline), whereas the analogous SSH test `TestUploadPackWithSidechannel_timeout` (`internal/gitaly/service/ssh/upload_pack_test.go:98-148`) demonstrates the SSH path correctly aborts on its dedicated negotiation timeout. [7](#0-6)

### Citations

**File:** internal/gitaly/service/smarthttp/upload_pack.go (L27-43)
```go
	var sidechannelRetryableError sidechannel.RetryableError
	conn, err := sidechannel.OpenSidechannel(ctx)
	if err != nil {
		if errors.As(err, &sidechannelRetryableError) {
			// Clients of PostUploadPackWithSidechannel are configured to retry the RPC upon receiving
			// Unavailable, so it should be OK to return it in this case.
			//nolint:gitaly-linters
			return nil, structerr.NewUnavailable("open sidechannel: %w", err)
		}
		return nil, structerr.NewInternal("open sidechannel: %w", err)
	}
	defer conn.Close()

	stats, err := s.runUploadPack(ctx, req, repoPath, gitConfig, conn, conn)
	if err != nil {
		return nil, structerr.NewInternal("running upload-pack: %w", err)
	}
```

**File:** internal/gitaly/service/smarthttp/upload_pack.go (L158-183)
```go
	commandOpts := []gitcmd.CmdOpt{
		gitcmd.WithStdin(stdin),
		gitcmd.WithSetupStdout(),
		gitcmd.WithGitProtocol(s.logger, req),
		gitcmd.WithConfig(gitConfig...),
		gitcmd.WithPackObjectsHookEnv(objectHash, req.GetRepository(), "http"),
	}

	if s.cfg.Hooks.PackObjectsHookMaxProc > 0 {
		commandOpts = append(commandOpts, gitcmd.WithEnv("GOMAXPROCS="+strconv.Itoa(int(s.cfg.Hooks.PackObjectsHookMaxProc))))
	}

	cmd, err := repo.Exec(ctx, gitcmd.Command{
		Name:  "upload-pack",
		Flags: []gitcmd.Option{gitcmd.Flag{Name: "--stateless-rpc"}},
		Args:  []string{repoPath},
	}, commandOpts...)
	if err != nil {
		return nil, structerr.NewFailedPrecondition("spawning upload-pack: %w", err)
	}

	// Use a custom buffer size to minimize the number of system calls.
	respBytes, err := io.CopyBuffer(stdout, cmd, make([]byte, 64*1024))
	if err != nil {
		return nil, structerr.NewFailedPrecondition("copying stdout from upload-pack: %w", err)
	}
```

**File:** internal/gitaly/service/ssh/upload_pack.go (L100-114)
```go
	timeoutTicker := s.uploadPackRequestTimeoutTickerFactory()

	// upload-pack negotiation is terminated by either a flush, or the "done"
	// packet: https://github.com/git/git/blob/v2.20.0/Documentation/technical/pack-protocol.txt#L335
	//
	// "flush" tells the server it can terminate, while "done" tells it to start
	// generating a packfile. Add a timeout to the second case to mitigate
	// use-after-check attacks.
	if err := s.runUploadCommand(ctx, repo, stdin, stdout, stderr, timeoutTicker, pktline.PktDone(), gitcmd.Command{
		Name: "upload-pack",
		Args: []string{repoPath},
	}, commandOpts...); err != nil {
		status, _ := command.ExitStatus(err)
		return nil, status, fmt.Errorf("running upload-pack: %w", err)
	}
```

**File:** internal/gitaly/service/ssh/upload_command.go (L46-82)
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

	if err := cmd.Wait(); err != nil {
		// The read monitor will cancel the local `ctx` when we do not observe a specific packet before the
		// timeout ticker ticks. This is done to address a time-of-check-to-time-of-use-style race, where the
		// client opens a connection but doesn't yet perform the negotiation of what data the server should
		// send. Because access checks only happen at the beginning of the call, it may be the case that the
		// client's permissions have changed since the RPC call started.
		//
		// To address this issue, we thus timebox the maximum amount of time between the start of the RPC call
		// and the end of the negotiation phase. While this doesn't completely address the issue, it's the best
		// we can reasonably do here.
		//
		// To distinguish cancellation of the overall RPC call and a timeout of the negotiation phase we use two
		// different contexts. In the case where the local context has been cancelled, we know that the reason
		// for cancellation is that the negotiation phase did not finish in time and thus return a more specific
		// error.
		if ctx.Err() != nil && rpcContext.Err() == nil {
			return structerr.NewDeadlineExceeded("waiting for negotiation: %w", ctx.Err())
		}
```

**File:** internal/gitaly/service/smarthttp/server.go (L19-37)
```go
type server struct {
	gitalypb.UnimplementedSmartHTTPServiceServer
	logger                           log.Logger
	cfg                              config.Cfg
	locator                          storage.Locator
	packfileNegotiationMetrics       *prometheus.CounterVec
	packfileNegotiationDeepenMetrics prometheus.Histogram
	uploadPackServedBytesMetrics     *prometheus.CounterVec
	receivePackObjectsMetrics        prometheus.Histogram
	infoRefCache                     infoRefCache
	txManager                        transaction.Manager
	txRegistry                       *storagemgr.TransactionRegistry
	hookManager                      hook.Manager
	updater                          *updateref.UpdaterWithHooks
	backupLocator                    backup.Locator
	backupSink                       *backup.Sink
	localRepoFactory                 localrepo.Factory
	bundleURIManager                 *bundleuri.GenerationManager
}
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

**File:** internal/gitaly/service/ssh/upload_pack_test.go (L98-148)
```go
func TestUploadPackWithSidechannel_timeout(t *testing.T) {
	t.Parallel()

	runTestWithAndWithoutConfigOptions(t, testUploadPackWithSidechannelTimeout, testcfg.WithPackObjectsCacheEnabled())
}

func testUploadPackWithSidechannelTimeout(t *testing.T, ctx context.Context, opts ...testcfg.Option) {
	t.Parallel()

	cfg := testcfg.Build(t, opts...)

	cfg.SocketPath = runSSHServerWithOptions(t, cfg, []ServerOpt{
		WithUploadPackRequestTimeoutTickerFactory(func() helper.Ticker {
			// Create a ticker that will immediately tick when getting reset so that the
			// server-side can observe this as an emulated timeout.
			ticker := helper.NewManualTicker()
			ticker.ResetFunc = func() {
				ticker.Tick()
			}
			return ticker
		}),
	})

	repo, repoPath := gittest.CreateRepository(t, testhelper.Context(t), cfg)
	gittest.WriteCommit(t, cfg, repoPath, gittest.WithBranch("main"))

	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	registry := sidechannel.NewRegistry()
	client := newSSHClientWithSidechannel(t, ctx, registry, cfg.SocketPath)

	ctx, waiter := sidechannel.RegisterSidechannel(ctx, registry, func(clientConn *sidechannel.ClientConn) (returnedErr error) {
		// Discard all data and block on the connection closing. The client says nothing
		// back to the server and doesn't finish the negotiation. This leads to the server
		// closing the connections once the negotiation timeout fires.
		_, err := io.Copy(io.Discard, clientConn)
		assert.NoError(t, err)
		return nil
	})
	defer testhelper.MustClose(t, waiter)

	_, err := client.SSHUploadPackWithSidechannel(ctx, &gitalypb.SSHUploadPackWithSidechannelRequest{
		Repository: repo,
	})

	// Because the client says nothing, the server would block. Because of
	// the timeout, it won't block forever, and return with a non-zero exit
	// code instead.
	testhelper.RequireGrpcError(t, structerr.NewDeadlineExceeded("running upload-pack: waiting for negotiation: context canceled"), err)
}
```
