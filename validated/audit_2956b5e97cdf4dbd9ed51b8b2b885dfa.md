Confirmed: neither `FindRemoteRootRef` nor `FindRemoteRepository` apply any per-RPC timeout or concurrency-limit middleware, unlike `FetchRemote`, which explicitly bounds its work via `req.GetTimeout()`.

### Title
Unbounded outbound git network operations against attacker-controlled remote URLs enable RPC handler DoS - (File: internal/gitaly/service/remote/find_remote_root_ref.go, internal/gitaly/service/remote/find_remote_repository.go)

### Summary
Both `FindRemoteRootRef` and `FindRemoteRepository` accept an arbitrary, caller-supplied remote URL and spawn a Git subprocess (`git remote show inmemory` / `git ls-remote`) to talk to that remote. Neither RPC imposes any server-side timeout or deadline on the outbound network operation, relying purely on whatever deadline the client happens to set on the gRPC context. If the client omits a deadline (or a chain of internal callers propagates a long-lived context), a malicious or unresponsive remote can keep the underlying `git` process — and the goroutine executing the RPC — blocked indefinitely.

### Finding Description
`s.findRemoteRootRefCmd` builds and executes `git remote show inmemory` against `request.GetRemoteUrl()` with the RPC's inbound `ctx` used verbatim, with no `context.WithTimeout` wrapper: [1](#0-0) 

Similarly, `FindRemoteRepository` executes `git ls-remote <remote> HEAD` using the bare inbound `ctx`: [2](#0-1) 

This is in stark contrast to the sibling `FetchRemote` RPC, which explicitly wraps its context with a caller-controllable timeout before doing any remote-facing work: [3](#0-2) 

Both `FindRemoteRootRef` and `FindRemoteRepository` proto messages have no timeout field of their own, so the only bound on execution time is whatever deadline (if any) the calling client attaches to the gRPC call. If an ordinary/unprivileged caller (or an internal service acting on unprivileged instructions) supplies a remote URL pointing at a slow-loris HTTP server, a TCP endpoint that accepts but never responds, or any endpoint under attacker control, the spawned `git` subprocess can hang indefinitely (e.g., waiting on the HTTP response headers or during the negotiation phase). This ties up a Gitaly worker goroutine and the underlying OS process/file descriptors for as long as the connection is held open, with no built-in cutoff, mirroring the “downloadS3Data hangs without timeout” bug class from the report — an outbound network call with no enforced timeout blocking a critical code path.

### Impact Explanation
Repeated or concurrent invocations with such attacker-controlled remote URLs can exhaust Gitaly's available worker goroutines/file descriptors/subprocess slots, since there is no per-call timeout to bound resource holding time (unlike `FetchRemote`). This is a resource-exhaustion / denial-of-service risk against the `RemoteService` RPC handlers, degrading availability for legitimate git operations on the affected Gitaly node.

### Likelihood Explanation
Both RPCs are reachable with a fully user/caller-controlled `remote_url` field and are commonly invoked as part of import/mirror workflows (e.g., GitLab import-by-URL, remote mirroring, root-ref discovery), so an attacker able to trigger these code paths with an arbitrary URL (or SSRF-style internal target) needs no special privileges beyond being able to submit such a request. The likelihood is moderate-to-high because it requires no authentication bypass — only supplying a URL under attacker control that is slow or unresponsive.

### Recommendation
Enforce a server-side timeout on the outbound git command context for `FindRemoteRootRef` and `FindRemoteRepository`, analogous to the pattern already used in `FetchRemote` (wrapping `ctx` with `context.WithTimeout` before executing `git ls-remote`/`git remote show`). Consider adding an explicit, bounded default (and optionally a request-level override, capped by a server-configured maximum) so that unresponsive remotes cannot indefinitely tie up Gitaly worker resources.

### Proof of Concept
1. Stand up an HTTP listener that accepts the connection but never writes a response (or writes headers extremely slowly), simulating an unresponsive/malicious remote — this mirrors the existing test harness pattern used in `fetch_remote_test.go`'s "http with timeout" case, which uses a blocking channel to simulate a stalled server: [4](#0-3) 
2. Call `FindRemoteRootRef` (or `FindRemoteRepository`) with `RemoteUrl` pointing at this listener, using a gRPC client context with no deadline (or a very long one).
3. Observe that the RPC never completes and the underlying `git remote show` / `git ls-remote` process remains running indefinitely, holding the goroutine and file descriptors open, since no server-side timeout exists to cut it off — unlike the analogous `FetchRemote` test case, which succeeds specifically because `Timeout: 1` bounds the operation.

### Citations

**File:** internal/gitaly/service/remote/find_remote_root_ref.go (L17-52)
```go
func (s *server) findRemoteRootRefCmd(ctx context.Context, request *gitalypb.FindRemoteRootRefRequest) (*command.Command, error) {
	remoteURL := request.GetRemoteUrl()
	var config []gitcmd.ConfigPair

	if resolvedAddress := request.GetResolvedAddress(); resolvedAddress != "" {
		modifiedURL, resolveConfig, err := gitcmd.GetURLAndResolveConfig(remoteURL, resolvedAddress)
		if err != nil {
			return nil, structerr.NewInvalidArgument("couldn't get curloptResolve config: %w", err)
		}

		remoteURL = modifiedURL
		config = append(config, resolveConfig...)
	}

	config = append(config, gitcmd.ConfigPair{Key: "remote.inmemory.url", Value: remoteURL})

	if authHeader := request.GetHttpAuthorizationHeader(); authHeader != "" {
		config = append(config, gitcmd.ConfigPair{
			Key:   fmt.Sprintf("http.%s.extraHeader", request.GetRemoteUrl()),
			Value: "Authorization: " + authHeader,
		})
	}

	repo := s.localRepoFactory.Build(request.GetRepository())

	return repo.Exec(ctx,
		gitcmd.Command{
			Name:   "remote",
			Action: "show",
			Args:   []string{"inmemory"},
		},
		gitcmd.WithDisabledHooks(),
		gitcmd.WithConfigEnv(config...),
		gitcmd.WithSetupStdout(),
	)
}
```

**File:** internal/gitaly/service/remote/find_remote_repository.go (L13-35)
```go
func (s *server) FindRemoteRepository(ctx context.Context, req *gitalypb.FindRemoteRepositoryRequest) (*gitalypb.FindRemoteRepositoryResponse, error) {
	if req.GetRemote() == "" {
		return nil, structerr.NewInvalidArgument("empty remote can't be checked.")
	}

	var output bytes.Buffer
	cmd, err := s.gitCmdFactory.NewWithoutRepo(ctx,
		gitcmd.Command{
			Name: "ls-remote",
			Args: []string{
				req.GetRemote(),
				"HEAD",
			},
		},
		gitcmd.WithStdout(&output),
	)
	if err != nil {
		return nil, structerr.NewInternal("error executing git command: %w", err)
	}

	if err := cmd.Wait(); err != nil {
		return &gitalypb.FindRemoteRepositoryResponse{Exists: false}, nil
	}
```

**File:** internal/gitaly/service/repository/fetch_remote.go (L30-39)
```go
func (s *server) FetchRemote(ctx context.Context, req *gitalypb.FetchRemoteRequest) (*gitalypb.FetchRemoteResponse, error) {
	if err := s.validateFetchRemoteRequest(ctx, req); err != nil {
		return nil, err
	}

	if req.GetTimeout() > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, time.Duration(req.GetTimeout())*time.Second)
		defer cancel()
	}
```

**File:** internal/gitaly/service/repository/fetch_remote_test.go (L1157-1184)
```go
		{
			desc: "http with timeout",
			setup: func(t *testing.T, cfg config.Cfg) setupData {
				_, remoteRepoPath := gittest.CreateRepository(t, ctx, cfg)
				repoProto, repoPath := gittest.CreateRepository(t, ctx, cfg)

				ch := make(chan bool)

				gitCmdFactory := gittest.NewCommandFactory(t, cfg)
				port := gittest.HTTPServer(t, ctx, gitCmdFactory, remoteRepoPath, func(w http.ResponseWriter, r *http.Request, next http.Handler) {
					<-ch
				})

				t.Cleanup(func() { close(ch) })

				return setupData{
					repoPath: repoPath,
					request: &gitalypb.FetchRemoteRequest{
						Repository: repoProto,
						RemoteParams: &gitalypb.Remote{
							Url:                     fmt.Sprintf("http://127.0.0.1:%d/%s", port, filepath.Base(remoteRepoPath)),
							HttpAuthorizationHeader: httpToken,
						},
						Timeout: 1,
					},
					runs: []run{{expectedErr: structerr.NewInternal("fetch remote: signal: killed: context deadline exceeded")}},
				}
			},
```
