### Title
`PostReceivePack` lacks the negotiation-timeout protection present in upload-pack/upload-archive, allowing an idle stream to hold the handler and a spawned git process open indefinitely - (File: `internal/gitaly/service/smarthttp/receive_pack.go`)

### Summary
`PostReceivePack` reads pack data directly from the gRPC stream into `git-receive-pack --stateless-rpc` via `streamio.NewReader`/`stream.Recv()`, with no idle/negotiation timeout enforced anywhere in the handler. In contrast, the SSH upload-pack and upload-archive handlers explicitly guard against exactly this scenario using `runUploadCommand` with a `pktline.NewReadMonitor` and a configurable timeout ticker.

### Finding Description
`postReceivePack` builds `stdin` straight from `stream.Recv()` and passes it to `repo.Exec(...)` running `git receive-pack --stateless-rpc`, then blocks on `cmd.Wait()`: [1](#0-0) 

There is no monitor, ticker, or context deadline installed around the read loop. If the client sends the first `PostReceivePackRequest` (satisfying `validateReceivePackRequest`, which only checks `GlId` and repository validity) and then never sends another message, `stream.Recv()` blocks indefinitely, `git-receive-pack` sits waiting on stdin, and the goroutine + child process remain alive for the lifetime of the underlying HTTP/2 stream: [2](#0-1) 

By contrast, `internal/gitaly/service/ssh/upload_command.go`'s `runUploadCommand` explicitly wraps stdin in a `pktline.NewReadMonitor` and starts a `monitor.Monitor` goroutine that cancels the command context if a specific packet (e.g., the negotiation-ending boundary) is not observed before a configurable ticker fires: [3](#0-2) 

This mechanism is deliberately documented as closing a "time-of-check-to-time-of-use" race and bounding negotiation time, and it is wired up for `upload-pack` and `upload-archive` (both SSH and, presumably, comparable smarthttp analogs) via configuration: [4](#0-3) 

Grepping the config layer confirms `upload_pack_negotiation` and `upload_archive_negotiation` timeout settings exist in `internal/gitaly/config/config.go` and `internal/gitaly/service/ssh/server.go`, but there is no equivalent `receive_pack_negotiation` timeout setting or usage anywhere in the codebase — `PostReceivePack` (smarthttp) has no analogous protection.

The gRPC server itself does not compensate for this: it only configures `KeepaliveEnforcementPolicy`/`KeepaliveParams` with `Time: 5 * time.Minute` (connection-level ping interval) and no `Timeout`, `MaxConnectionAge`, or `MaxConnectionIdle` setting: [5](#0-4) 

An HTTP/2-compliant client (or a low-level client crafted specifically to answer keepalive PINGs while never sending stream data) can keep the TCP/HTTP2 connection healthy indefinitely while never advancing the RPC's application-level data, so the keepalive settings do not bound this attack. Per-RPC concurrency limiting (`[[concurrency]]`) that could cap simultaneous `PostReceivePack` invocations per repository is off by default — it is shown only as a commented-out example in `config.toml.example`: [6](#0-5) 

### Impact Explanation
An unprivileged user who can push to a repository they own can open one or more `PostReceivePack` streams, send only the initial metadata request, and then stall indefinitely. Each such stream ties up a Gitaly gRPC handler goroutine and a spawned `git receive-pack --stateless-rpc` child process (plus its memory/FD footprint) for as long as the stream is kept alive, which — absent any negotiation timeout and absent a default concurrency limit — is effectively unbounded. Repeating this from multiple pushes/connections can exhaust goroutines, process slots, or memory on the Gitaly node, degrading or denying service for that node's repositories. This matches the "DoS / resource exhaustion or handler crash on default configuration" impact class.

### Likelihood Explanation
The precondition is simply push access to a repository the attacker owns, which any unprivileged GitLab user has by default (fork/own-project push). No secret, admin role, or non-default configuration is required. The exploit only requires opening a `PostReceivePack` stream, sending the first metadata request, and withholding further data (or pack bytes without a terminating flush), which is fully within an unprivileged client's control and trivially repeatable/scriptable.

### Recommendation
Apply the same negotiation-timeout pattern used in `internal/gitaly/service/ssh/upload_command.go` to `PostReceivePack` (and its SSH `receive-pack` counterpart): wrap the request stdin in a `pktline.NewReadMonitor`-style monitor bound to a configurable ticker (e.g., a new `receive_pack_negotiation` timeout under `[timeout]`), cancel the command's `context.Context` if no forward progress (e.g., no flush/data) is observed within the timeout, and ensure a default concurrency limit exists for `PostReceivePack` so that a bounded number of stalled streams per repository can be enforced even before any timeout fires.

### Proof of Concept
```go
func TestPostReceivePack_StalledStreamNeverTimesOut(t *testing.T) {
    cfg := testcfg.Build(t)
    // ... start smarthttp server as in receive_pack_test.go setup ...

    repo, _ := gittest.CreateRepository(t, ctx, cfg)
    client := newSmartHTTPClient(t, serverSocketPath, cfg.Auth.Token)

    stream, err := client.PostReceivePack(ctx)
    require.NoError(t, err)

    require.NoError(t, stream.Send(&gitalypb.PostReceivePackRequest{
        Repository: repo,
        GlId:       "user-123",
    }))

    // Attacker sends nothing further: no pack data, no flush-pkt, stream stays open.
    // Expected (vulnerable) behavior: stream.Recv() blocks indefinitely with no
    // deadline exceeded error, unlike TestFailedUploadArchiveRequestDueToTimeout
    // for SSHUploadArchive, which does observe a bounded DeadlineExceeded error.
    done := make(chan error, 1)
    go func() {
        _, err := stream.Recv()
        done <- err
    }()

    select {
    case <-done:
        t.Fatal("did not expect stream to resolve without client sending more data")
    case <-time.After(30 * time.Second):
        // Handler is still blocked holding the goroutine and git-receive-pack process open;
        // no negotiation timeout fired (contrast with upload_archive_test.go's
        // TestFailedUploadArchiveRequestDueToTimeout, which uses a ticker to bound this).
    }
}
```
This contrasts directly with the existing `TestFailedUploadArchiveRequestDueToTimeout` test [7](#0-6) , which demonstrates that upload-archive *is* protected by a timeout ticker while `PostReceivePack` has no equivalent test or mechanism.

### Citations

**File:** internal/gitaly/service/smarthttp/receive_pack.go (L75-145)
```go
	stdin := streamio.NewReader(func() ([]byte, error) {
		resp, err := stream.Recv()
		return resp.GetData(), err
	})

	if featureflag.ReceivePackStats.IsEnabled(ctx) {
		// The deferred call runs after cmd.Wait() has drained stdin, so the
		// capture is complete by the time it is read.
		statsCapture := &stats.PrefixCapture{}
		defer func() {
			stats.RecordReceivePack(ctx, s.logger, statsCapture.Bytes(), s.receivePackObjectsMetrics)
		}()
		stdin = io.TeeReader(stdin, statsCapture)
	}

	stdout := streamio.NewWriter(func(p []byte) error {
		return stream.Send(&gitalypb.PostReceivePackResponse{Data: p})
	})

	repo := s.localRepoFactory.Build(req.GetRepository())

	repoPath, err := repo.Path(ctx)
	if err != nil {
		return err
	}

	config, err := gitcmd.ConvertConfigOptions(req.GetGitConfigOptions())
	if err != nil {
		return err
	}

	transactionID := storage.ExtractTransactionID(ctx)
	transactionsEnabled := transactionID > 0
	if transactionsEnabled {
		procReceiveCleanup, err := receivepack.RegisterProcReceiveHook(
			ctx, s.logger, s.cfg, req, repo, s.hookManager, hook.NewTransactionRegistry(s.txRegistry), transactionID,
		)
		if err != nil {
			return err
		}
		defer func() {
			if err := procReceiveCleanup(); err != nil && returnedErr == nil {
				returnedErr = err
			}
		}()
	}

	objectHash, err := repo.ObjectHash(ctx)
	if err != nil {
		return fmt.Errorf("detecting object hash: %w", err)
	}

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

**File:** internal/gitaly/service/smarthttp/receive_pack.go (L150-162)
```go
func validateReceivePackRequest(ctx context.Context, locator storage.Locator, req *gitalypb.PostReceivePackRequest) error {
	if req.GetGlId() == "" {
		return structerr.NewInvalidArgument("empty GlId")
	}
	if req.Data != nil {
		return structerr.NewInvalidArgument("non-empty Data")
	}
	if err := locator.ValidateRepository(ctx, req.GetRepository()); err != nil {
		return structerr.NewInvalidArgument("%w", err)
	}

	return nil
}
```

**File:** internal/gitaly/service/ssh/upload_command.go (L26-82)
```go
func (s *server) runUploadCommand(
	rpcContext context.Context,
	repo gitcmd.RepositoryExecutor,
	stdin io.Reader,
	stdout, stderr io.Writer,
	timeoutTicker helper.Ticker,
	boundaryPacket []byte,
	sc gitcmd.Command,
	opts ...gitcmd.CmdOpt,
) error {
	ctx, cancelCtx := context.WithCancel(rpcContext)
	defer cancelCtx()

	var stderrBuilder strings.Builder
	stderr = io.MultiWriter(stderr, &stderrBuilder)

	stdoutCounter := &helper.CountingWriter{W: stdout}
	// Use large copy buffer to reduce the number of system calls
	stdout = &largeBufferReaderFrom{Writer: stdoutCounter}

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

**File:** config.toml.example (L118-126)
```text
# # You can adjust the concurrency of each RPC endpoint
# [[concurrency]]
# # Name of the RPC endpoint.
# rpc = "/gitaly.RepositoryService/OptimizeRepository"
# # Concurrency per RPC per repository.
# max_per_repo = 1
# max_queue_wait = "1m"
# max_queue_size = 10

```

**File:** config.toml.example (L194-197)
```text
# # Negotiation timeouts for remote Git operations
# [timeout]
# upload_pack_negotiation = "10m"
# upload_archive_negotiation = "1m"
```

**File:** internal/gitaly/server/server.go (L220-243)
```go
	serverOptions := []grpc.ServerOption{
		grpc.StatsHandler(tracing.NewGRPCServerStatsHandler(
			otelgrpc.WithTracerProvider(otel.GetTracerProvider()),
		)),
		grpc.StatsHandler(loghandler.PerRPCLogHandler{
			Underlying:     &grpcstats.PayloadBytes{},
			FieldProducers: []loghandler.FieldsProducer{grpcstats.FieldsProducer},
		}),
		grpc.Creds(lm),
		grpc.ChainStreamInterceptor(streamServerInterceptors...),
		grpc.ChainUnaryInterceptor(unaryServerInterceptors...),
		// We deliberately set the server MinTime to significantly less than the client interval of 20
		// seconds to allow for network jitter. We can afford to be forgiving as the maximum number of
		// concurrent clients for a Gitaly server is typically in the hundreds and this volume of
		// keepalives won't add significant load.
		grpc.KeepaliveEnforcementPolicy(keepalive.EnforcementPolicy{
			MinTime:             10 * time.Second,
			PermitWithoutStream: true,
		}),
		grpc.KeepaliveParams(keepalive.ServerParameters{
			Time: 5 * time.Minute,
		}),
		grpc.WaitForHandlers(false),
	}
```

**File:** internal/gitaly/service/ssh/upload_archive_test.go (L20-75)
```go
func TestFailedUploadArchiveRequestDueToTimeout(t *testing.T) {
	t.Parallel()

	cfg := testcfg.Build(t)

	// Use a ticker channel so that we can observe that the ticker is being created. The channel
	// is unbuffered on purpose so that we can assert that it is getting created exactly at the
	// time we expect it to be.
	tickerCh := make(chan *helper.ManualTicker)

	cfg.SocketPath = runSSHServerWithOptions(t, cfg, []ServerOpt{
		WithArchiveRequestTimeoutTickerFactory(func() helper.Ticker {
			// Create a ticker that will immediately tick when getting reset so that the
			// server-side can observe this as an emulated timeout.
			ticker := helper.NewManualTicker()
			ticker.ResetFunc = func() {
				ticker.Tick()
			}
			tickerCh <- ticker
			return ticker
		}),
	})

	ctx := testhelper.Context(t)
	repo, _ := gittest.CreateRepository(t, ctx, cfg)

	client := newSSHClient(t, cfg.SocketPath)

	stream, err := client.SSHUploadArchive(ctx)
	require.NoError(t, err)

	// The first request is not limited by timeout, but also not under attacker control
	require.NoError(t, stream.Send(&gitalypb.SSHUploadArchiveRequest{Repository: repo}))

	// We should now see that the ticker limiting the request is being created. We don't need to
	// use the ticker, but this statement is only there in order to verify that the ticker is
	// indeed getting created at the expected point in time.
	<-tickerCh

	// Because the client says nothing, the server would block. Because of
	// the timeout, it won't block forever, and return with a non-zero exit
	// code instead.
	requireFailedSSHStream(t, structerr.NewDeadlineExceeded("running upload-archive: waiting for negotiation: context canceled"), func() (int32, error) {
		resp, err := stream.Recv()
		if err != nil {
			return 0, err
		}

		var code int32
		if status := resp.GetExitStatus(); status != nil {
			code = status.GetValue()
		}

		return code, nil
	})
}
```
