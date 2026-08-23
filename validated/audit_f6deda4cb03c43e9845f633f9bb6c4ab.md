### Title
`PostUploadPackWithSidechannel` lacks the negotiation timeout that `SSHUploadPackWithSidechannel` enforces, allowing an unbounded stalled-negotiation DoS - (File: internal/gitaly/service/smarthttp/upload_pack.go)

### Summary
`PostUploadPackWithSidechannel` pipes the sidechannel connection directly into `git upload-pack --stateless-rpc` as stdin/stdout with no negotiation timeout, unlike the SSH equivalent (`SSHUploadPackWithSidechannel`/`sshUploadPack`) which explicitly wraps the same operation with a `uploadPackRequestTimeoutTickerFactory`-based timeout specifically to defend against a client that never sends `done` or a flush packet. An attacker-controlled client that opens the sidechannel and simply never writes a flush/`done` packet can keep the Gitaly-side subprocess and gRPC handler blocked indefinitely.

### Finding Description
`PostUploadPackWithSidechannel` (`internal/gitaly/service/smarthttp/upload_pack.go:21-52`) opens a sidechannel via `sidechannel.OpenSidechannel(ctx)` and calls `s.runUploadPack(ctx, req, repoPath, gitConfig, conn, conn)` [1](#0-0) . Inside `runUploadPack`, the sidechannel connection is used directly as stdin for a spawned `git upload-pack --stateless-rpc` process via `gitcmd.WithStdin(stdin)`, and the handler blocks on `io.CopyBuffer(stdout, cmd, ...)` followed by `cmd.Wait()` with no timeout applied to the underlying negotiation [2](#0-1) .

By contrast, the SSH transport's equivalent code path, `sshUploadPack` in `internal/gitaly/service/ssh/upload_pack.go`, explicitly creates a `timeoutTicker := s.uploadPackRequestTimeoutTickerFactory()` and passes it into `s.runUploadCommand(...)` together with `pktline.PktDone()`, with the comment: "upload-pack negotiation is terminated by either a flush, or the 'done' packet ... Add a timeout to the second case to mitigate use-after-check attacks." [3](#0-2) . This protection exists precisely because git's upload-pack negotiation reads from stdin until it sees a `done` or flush packet, and a client controlling that stream can withhold it forever. `grep` for `RequestTimeoutTicker`/`UploadPackRequestTimeout` across the repo only returns matches under `internal/gitaly/service/ssh/`, confirming the smarthttp path has no analogous mechanism.

No middleware compensates for this: the gRPC server sets `KeepaliveEnforcementPolicy`/`KeepaliveParams` only to detect dead TCP connections, not to bound the duration of an idle-but-alive negotiation, and no unary interceptor imposes a request deadline [4](#0-3) . Because the attacker is an unprivileged user who can drive `PostUploadPackWithSidechannel` through a normal git-over-HTTP fetch/clone request (via Workhorse forwarding to Gitaly), they fully control the sidechannel byte stream and can simply keep the connection open without ever sending a flush/`done` pktline.

### Impact Explanation
Each such stalled request keeps a subprocess (`git upload-pack --stateless-rpc`) and a gRPC handler goroutine alive indefinitely, consuming file descriptors, memory, and a concurrency slot. Repeating this from a single unprivileged account can exhaust concurrency limits or process/goroutine resources on the Gitaly node, producing a DoS against the `PostUploadPackWithSidechannel` handler (and, by extension, other clients competing for the same repository/host resources) — matching the GitLab HackerOne "DoS / resource exhaustion" impact class described in the question.

### Likelihood Explanation
Any GitLab user with fetch/clone access to a repository they can reach (including their own fork) can trigger `PostUploadPackWithSidechannel` via the standard git-over-HTTP protocol; no elevated privileges, secrets, or non-default configuration are required. The exploit only requires controlling the client side of an ordinary git-http-clone-like session and withholding the terminating flush/`done` packet, which is straightforward with a raw pktline client (as already demonstrated by the analogous SSH test `TestUploadPackWithSidechannelTimeout` in `internal/gitaly/service/ssh/upload_pack_test.go:98-148`, which simulates exactly this scenario against the SSH RPC and asserts that the SSH path degrades gracefully via `DeadlineExceeded`). The corresponding smarthttp test/protection does not exist, so repeatability is high and the barrier to entry is low.

### Recommendation
Apply the same negotiation-timeout mechanism used in `internal/gitaly/service/ssh/upload_pack.go` (the `uploadPackRequestTimeoutTickerFactory`/`runUploadCommand` pattern with `pktline.PktDone()`) to `runUploadPack` in `internal/gitaly/service/smarthttp/upload_pack.go`, so that a stalled or incomplete negotiation stream is bounded by a timeout and the handler/subprocess are torn down instead of blocking indefinitely.

### Proof of Concept
```go
// internal/gitaly/service/smarthttp/upload_pack_test.go (illustrative)
func TestPostUploadPackWithSidechannel_stalledNegotiation(t *testing.T) {
    cfg, repo, _ := setupSmartHTTPServiceWithRuby... // existing test setup pattern

    registry := sidechannel.NewRegistry()
    client := newSmartHTTPClientWithSidechannel(t, ctx, registry, cfg.SocketPath)

    ctx, waiter := sidechannel.RegisterSidechannel(ctx, registry, func(clientConn *sidechannel.ClientConn) error {
        // Attacker never sends a flush or "done" packet; just holds the
        // connection open indefinitely.
        <-ctx.Done() // simulate holding forever / long block
        return nil
    })
    defer testhelper.MustClose(t, waiter)

    _, err := client.PostUploadPackWithSidechannel(ctx, &gitalypb.PostUploadPackWithSidechannelRequest{
        Repository: repo,
    })
    // Expected (desired) behavior: DeadlineExceeded after a bounded negotiation timeout,
    // matching ssh's `structerr.NewDeadlineExceeded("running upload-pack: waiting for negotiation: context canceled")`.
    // Actual (current) behavior: the call blocks until the client/context is externally
    // cancelled or the connection is forcibly closed — no server-side timeout fires.
}
```
This mirrors the existing `testUploadPackWithSidechannelTimeout` test for the SSH service (`internal/gitaly/service/ssh/upload_pack_test.go:98-148`) which confirms the SSH path is protected; the smarthttp equivalent test/protection does not exist in `internal/gitaly/service/smarthttp/upload_pack.go`, confirming the gap. [5](#0-4)

### Citations

**File:** internal/gitaly/service/smarthttp/upload_pack.go (L21-43)
```go
func (s *server) PostUploadPackWithSidechannel(ctx context.Context, req *gitalypb.PostUploadPackWithSidechannelRequest) (*gitalypb.PostUploadPackWithSidechannelResponse, error) {
	repoPath, gitConfig, err := s.validateUploadPackRequest(ctx, req)
	if err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}

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

**File:** internal/gitaly/service/smarthttp/upload_pack.go (L158-196)
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

	if err := cmd.Wait(); err != nil {
		stats = collector.finish()
		if _, ok := command.ExitStatus(err); ok && stats.Deepen != "" {
			// We have seen a 'deepen' message in the request. It is expected that
			// git-upload-pack has a non-zero exit status: don't treat this as an
			// error.
			s.recordServedBytes(stats, respBytes)
			return stats, nil
		}

		return nil, structerr.NewFailedPrecondition("waiting for upload-pack: %w", err)
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

**File:** internal/gitaly/server/server.go (L235-242)
```go
		grpc.KeepaliveEnforcementPolicy(keepalive.EnforcementPolicy{
			MinTime:             10 * time.Second,
			PermitWithoutStream: true,
		}),
		grpc.KeepaliveParams(keepalive.ServerParameters{
			Time: 5 * time.Minute,
		}),
		grpc.WaitForHandlers(false),
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
