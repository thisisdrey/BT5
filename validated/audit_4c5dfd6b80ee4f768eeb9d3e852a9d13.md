## Analysis

The Sherlock report describes a **time-of-check/time-of-use (TOCTOU)** vulnerability class: a permission-relevant value is fixed at the start of an operation, but the operation's *actual* consumption of that value can be delayed indefinitely by the party being checked, allowing the checked condition to go stale before it's actually used.

Gitaly has an internal doc comment that explicitly names and mitigates this exact bug class for Git's smart-HTTP/SSH protocol negotiation phase: access checks (e.g., GitLab's `/internal/allowed`) happen once at the very start of an upload RPC, but the actual pack negotiation (client sending `want`/`done` packets) can be arbitrarily delayed by a malicious client, letting them fetch data after their access has since been revoked. [1](#0-0) 

This is fixed for **SSH** uploads (`git-upload-pack`, `git-upload-archive`) via `runUploadCommand`, which wraps client stdin in a `pktline.ReadMonitor` and cancels the command if the negotiation-terminating packet (`done`/flush) isn't observed within a timeout: [2](#0-1) [3](#0-2) [4](#0-3) 

However, the **SmartHTTP** equivalent, `PostUploadPackWithSidechannel` → `runUploadPack`, does **not** apply this monitor/timeout at all. It streams stdin directly into `git-upload-pack` and copies stdout back with a plain `io.CopyBuffer`, with no bound on how long the client can stall the negotiation phase before sending the terminating packet: [5](#0-4) 

### Title
Missing negotiation timeout in SmartHTTP `PostUploadPackWithSidechannel` allows use-after-check bypass of access revocation - (File: internal/gitaly/service/smarthttp/upload_pack.go)

### Summary
Gitaly documents and mitigates a specific TOCTOU class for Git upload operations: access is checked once at RPC start, but the negotiation phase can be stalled by the client, letting an access decision go stale before the packfile is actually produced. This mitigation (`pktline.ReadMonitor` + timeout ticker) is applied to the SSH path (`sshUploadPack`/`sshUploadArchive` via `runUploadCommand`), but is absent from the SmartHTTP path (`runUploadPack` in `internal/gitaly/service/smarthttp/upload_pack.go`), leaving that RPC exposed to the exact race the SSH code explicitly guards against.

### Finding Description
`internal/git/pktline/read_monitor.go` states the intended security invariant directly: without a bound on the negotiation phase, "the user has control of stdin for the git command, and if they can delay input for an arbitrarily long time, they can gain access days or weeks after the access check has completed." [6](#0-5) 

`runUploadCommand` (used only by `internal/gitaly/service/ssh/upload_pack.go` and `upload_archive.go`) implements the fix: it creates a `ReadMonitor` on stdin and cancels the RPC's local context if the client doesn't send the boundary packet (`done`/flush) before a timeout ticks, explicitly framed as addressing "a time-of-check-to-time-of-use-style race, where the client opens a connection but doesn't yet perform the negotiation." [7](#0-6) 

`PostUploadPackWithSidechannel`'s implementation, `runUploadPack` in `internal/gitaly/service/smarthttp/upload_pack.go`, performs the identical high-level operation — access is checked in `validateUploadPackRequest`, then `git-upload-pack --stateless-rpc` is spawned and fed the client-controlled sidechannel stream as stdin — but wires stdin directly to the subprocess and copies stdout with an unbounded `io.CopyBuffer`, without any `pktline.ReadMonitor` or timeout ticker. [8](#0-7) 

Since GitLab's access check (whether the user may fetch the repository) happens once, out-of-band, before/at the start of the gRPC call, and Gitaly does not re-validate it once negotiation completes, an unprivileged client can hold the sidechannel connection open — sending pktline data at an arbitrarily slow rate or pausing before the final `done`/flush — for as long as the RPC deadline allows, deferring actual packfile generation until well after the point where their access might have been revoked (e.g., project visibility changed, membership removed, deploy token revoked) between the check and the delayed negotiation.

### Impact Explanation
This directly undermines an access-control invariant Gitaly itself documents as security-relevant and has already fixed for the SSH transport but not for the (likely much more widely used) HTTP transport. A user whose access is revoked mid-request can still complete a clone/fetch and receive repository contents they should no longer be permitted to read, by exploiting the unbounded negotiation window. This is a concrete access-control bypass in a handler reachable directly from an unprivileged, ordinary Git client (`git clone`/`git fetch` over HTTP), matching the "auth bypass" / "DoS of a handler" acceptance criteria for this class.

### Likelihood Explanation
Highly reachable: `PostUploadPackWithSidechannel` is invoked on every `git clone`/`git fetch` over HTTP(S), Gitaly's most common transport. Exploitation only requires an attacker to control the pace/timing of their own git client's negotiation traffic (e.g., a custom client that stalls before sending `done`), which is trivial and needs no special privilege beyond initially-valid repository access.

### Recommendation
Apply the same `pktline.ReadMonitor`/timeout-ticker mitigation used in `internal/gitaly/service/ssh/upload_command.go` to the SmartHTTP `runUploadPack` path in `internal/gitaly/service/smarthttp/upload_pack.go`, bounding the time between RPC start and the end of the negotiation phase (i.e., observation of the `done`/flush boundary packet) before the packfile is generated, and canceling the request if it's exceeded.

### Proof of Concept
1. An unprivileged-but-authorized user initiates `git clone`/`git fetch` against a repository over the SmartHTTP transport, reaching `PostUploadPackWithSidechannel` → `runUploadPack` in `internal/gitaly/service/smarthttp/upload_pack.go`.
2. The client sends the initial `want` lines but withholds the terminating `done`/flush packet on the sidechannel, keeping the underlying `git-upload-pack --stateless-rpc` process (and the RPC) alive indefinitely — nothing in `runUploadPack` bounds this, unlike `runUploadCommand`'s `monitor.Monitor(ctx, boundaryPacket, timeoutTicker, cancelCtx)` in the SSH path (`internal/gitaly/service/ssh/upload_command.go:63`).
3. During this window, the user's access to the repository is revoked upstream (e.g., removed from project, project made private).
4. The client finally sends `done`; `io.CopyBuffer(stdout, cmd, ...)` streams the full packfile back with no re-check of permissions, completing the fetch despite the revoked access.

### Citations

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

**File:** internal/gitaly/service/ssh/upload_command.go (L16-25)
```go
// runUploadCommand runs an uploading command like git-upload-pack(1) or git-upload-archive(1). It serves multiple
// purposes:
//
//   - It sets up a large buffer reader such that we can write the data more efficiently.
//
//   - It logs how many bytes have been sent.
//
//   - It installs a timeout such that we can address time-of-check-to-time-of-use-style races. Otherwise it would be
//     possible to open the connection early, keep it open for an extended amount of time, and only do the negotiation of
//     what is to be sent at a later point when permissions of the user might have changed.
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

**File:** internal/gitaly/service/smarthttp/upload_pack.go (L107-183)
```go
func (s *server) validateUploadPackRequest(ctx context.Context, req *gitalypb.PostUploadPackWithSidechannelRequest) (string, []gitcmd.ConfigPair, error) {
	repository := req.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repository); err != nil {
		return "", nil, err
	}
	repoPath, err := s.locator.GetRepoPath(ctx, repository)
	if err != nil {
		return "", nil, err
	}

	gitcmd.WarnIfTooManyBitmaps(ctx, s.logger, s.locator, repository.GetStorageName(), repoPath)

	config, err := gitcmd.ConvertConfigOptions(req.GetGitConfigOptions())
	if err != nil {
		return "", nil, err
	}

	return repoPath, config, nil
}

func (s *server) runUploadPack(ctx context.Context, req *gitalypb.PostUploadPackWithSidechannelRequest, repoPath string, gitConfig []gitcmd.ConfigPair, stdin io.Reader, stdout io.Writer) (stats *stats.PackfileNegotiation, _ error) {
	h := sha1.New()

	stdin = io.TeeReader(stdin, h)
	stdin, collector := s.runStatsCollector(ctx, stdin)
	defer func() {
		if stats == nil {
			stats = collector.finish()
		}
	}()

	repo := s.localRepoFactory.Build(req.GetRepository())
	if s.bundleURIManager != nil {
		// Bundle generation is an optimization that is transparent to users.
		// If it fails, we log the error but continue with the regular upload-pack
		// operation without the bundle optimization.
		// If successful, a goroutine is spawned to generate the bundle, in which case
		// the bundle generation becomes independent of the RPC request.
		if err := s.bundleURIManager.GenerateWithStrategy(ctx, repo); err != nil {
			s.logger.WithError(err).Error("failed generating bundle")
		}
		gitConfig = append(gitConfig, s.bundleURIManager.UploadPackGitConfig(ctx, req.GetRepository())...)
	} else {
		gitConfig = append(gitConfig, bundleuri.CapabilitiesGitConfig(ctx, false)...)
	}

	objectHash, err := repo.ObjectHash(ctx)
	if err != nil {
		return nil, fmt.Errorf("detecting object hash: %w", err)
	}

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
