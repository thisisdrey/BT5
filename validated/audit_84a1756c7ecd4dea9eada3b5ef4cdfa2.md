### Title
Unbounded tar extraction in `SetCustomHooks`/`RestoreCustomHooks` allows disk/inode exhaustion via tar bomb - ([File: internal/gitaly/repoutil/custom_hooks.go])

### Summary
`ExtractHooks` pipes the client-supplied tar stream directly into `tar -xf -` with no size, member-count, or decompressed-size limit, and `SetCustomHooks` writes the result to a temp directory before an atomic rename into the repository. A user with push/import access can call `SetCustomHooks` (or the deprecated `RestoreCustomHooks`) repeatedly and/or concurrently with maliciously crafted tar archives (highly duplicated/compressible members) to exhaust Gitaly host disk space or inodes.

### Finding Description
`ExtractHooks` only peeks the stream to check for emptiness and then execs `tar -xf -` against the reader with no cap on the number of bytes or files extracted: [1](#0-0) 

`SetCustomHooks` (invoked by both `SetCustomHooks` and the deprecated `RestoreCustomHooks` RPC handlers) creates a temp directory via `tempdir.NewWithoutContext` and calls `ExtractHooks` into it before atomically renaming into the repository — no byte quota or repository-size check is performed anywhere in this path: [2](#0-1) 

Both RPC handlers stream the request body directly from the client into this function without any application-level size cap: [3](#0-2) 

The only available mitigation, `LimiterMiddleware`/`ConcurrencyLimiter`, is opt-in per-RPC configuration (`cfg.Concurrency`), not a default-enabled protection, and even when configured it limits concurrency, not the total bytes written per call: [4](#0-3) 

gRPC's default max receive message size bounds a single stream message but not the total number of messages a client can send in a client-streaming RPC, so an attacker can send an effectively unbounded amount of tar data across many stream messages, and repeat the RPC concurrently against the same or different repositories they control.

### Impact Explanation
Repeated/concurrent invocation with tar-bomb payloads can fill the Gitaly storage volume's disk space and/or inode table, degrading or crashing the Gitaly node for all repositories on that storage shard — a host-wide denial of service, matching the DoS-of-an-RPC-handler impact class in scope.

### Likelihood Explanation
`SetCustomHooks` is a `MUTATOR` RPC reachable by any client with write access to a repository (e.g., via project import/mirror or backup-restore flows that call this RPC on the user's behalf), and it is a client-streaming RPC that accepts arbitrary-length tar payload data. No authentication beyond normal repo write access is required, and the operation can be repeated indefinitely and run concurrently, making this readily and repeatably exploitable by an unprivileged but write-capable user.

### Recommendation
Enforce a maximum extracted size/member count in `ExtractHooks` (e.g., stream through a size-limited reader or `io.LimitReader`, reject archives exceeding a configurable byte/file-count threshold, and validate cumulative decompressed size during extraction rather than relying solely on `tar`). Additionally, enable default concurrency/rate limiting for `SetCustomHooks`/`RestoreCustomHooks` and consider enforcing a hard byte cap on the incoming stream at the gRPC handler level before invoking `repoutil.SetCustomHooks`.

### Proof of Concept
```go
func TestSetCustomHooks_tarBombExhaustsDisk(t *testing.T) {
    ctx := testhelper.Context(t)
    cfg, client := setupServer(t)
    repo, _ := gittest.CreateRepository(t, ctx, cfg)

    stream, err := client.SetCustomHooks(ctx)
    require.NoError(t, err)

    // Build a tar with many members of highly compressible/duplicated large content
    // under "custom_hooks/", e.g. thousands of 100MB files of repeated zero bytes,
    // sent across many stream chunks with no total-size cap enforced server-side.
    writer := streamio.NewWriter(func(p []byte) error {
        return stream.Send(&gitalypb.SetCustomHooksRequest{Repository: repo, Data: p})
    })
    writeTarBomb(t, writer) // writes GBs of tar data with duplicated content

    _, err = stream.CloseAndRecv()
    require.NoError(t, err)
    // Assert: server disk usage under repo's storage grows unbounded with no
    // rejection, demonstrating absence of any size cap in ExtractHooks/SetCustomHooks.
}
```
Running many such streams concurrently against the same or multiple repositories is expected to exhaust disk space/inodes on the Gitaly storage shard, since `ExtractHooks` in `internal/gitaly/repoutil/custom_hooks.go` performs no size validation before or during extraction.

### Citations

**File:** internal/gitaly/repoutil/custom_hooks.go (L55-77)
```go
func ExtractHooks(ctx context.Context, logger log.Logger, reader io.Reader, path string, stripPrefix bool) error {
	// GNU tar does not accept an empty file as a valid tar archive and produces
	// an error. Since an empty hooks tar is symbolic of a repository having no
	// hooks, the reader is peeked to check if there is any data present.
	buf := bufio.NewReader(reader)
	if _, err := buf.Peek(1); errors.Is(err, io.EOF) {
		return nil
	}

	stripComponents := "0"
	if stripPrefix {
		stripComponents = "1"
	}

	cmdArgs := []string{"-xf", "-", "-C", path, "--strip-components", stripComponents, CustomHooksDir}

	var stderrBuilder strings.Builder
	cmd, err := command.New(ctx, logger, append([]string{"tar"}, cmdArgs...),
		command.WithStdin(buf),
		command.WithStderr(&stderrBuilder))
	if err != nil {
		return fmt.Errorf("executing tar command: %w", err)
	}
```

**File:** internal/gitaly/repoutil/custom_hooks.go (L159-176)
```go
	// Create a temporary directory to write the new hooks to and also
	// temporarily store the current repository hooks. This enables "atomic"
	// directory swapping by acting as an intermediary storage location between
	// moves.
	tmpDir, err := tempdir.NewWithoutContext(repo.GetStorageName(), logger, locator)
	if err != nil {
		return fmt.Errorf("creating temp directory: %w", err)
	}

	defer func() {
		if err := os.RemoveAll(tmpDir.Path()); err != nil {
			logger.WithError(err).WarnContext(ctx, "failed to remove temporary directory")
		}
	}()

	if err := ExtractHooks(ctx, logger, reader, tmpDir.Path(), false); err != nil {
		return fmt.Errorf("extracting hooks: %w", err)
	}
```

**File:** internal/gitaly/service/repository/set_custom_hooks.go (L13-42)
```go
func (s *server) SetCustomHooks(stream gitalypb.RepositoryService_SetCustomHooksServer) error {
	ctx := stream.Context()

	firstRequest, err := stream.Recv()
	if err != nil {
		return structerr.NewInternal("getting first request: %w", err)
	}

	repo := firstRequest.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repo); err != nil {
		return structerr.NewInvalidArgument("%w", err)
	}

	reader := streamio.NewReader(func() ([]byte, error) {
		if firstRequest != nil {
			data := firstRequest.GetData()
			firstRequest = nil
			return data, nil
		}

		request, err := stream.Recv()
		return request.GetData(), err
	})

	if err := repoutil.SetCustomHooks(ctx, s.logger, s.locator, s.txManager, reader, repo); err != nil {
		return structerr.NewInternal("setting custom hooks: %w", err)
	}

	return stream.SendAndClose(&gitalypb.SetCustomHooksResponse{})
}
```

**File:** internal/grpc/middleware/limithandler/middleware.go (L172-211)
```go
// WithConcurrencyLimiters sets up middleware to limit the concurrency of
// requests based on RPC and repository
func WithConcurrencyLimiters(cfg config.Cfg) (map[string]*limiter.AdaptiveLimit, map[string]*limiter.AdaptiveLimit, SetupFunc) {
	perRPCLimits := map[string]*limiter.AdaptiveLimit{}
	perRPCLimitsUnauthenticated := map[string]*limiter.AdaptiveLimit{}

	for _, concurrency := range cfg.Concurrency {
		// Create authenticated limiter
		limitName := fmt.Sprintf("perRPC%s", concurrency.RPC)
		if concurrency.Adaptive {
			perRPCLimits[concurrency.RPC] = limiter.NewAdaptiveLimit(limitName, limiter.AdaptiveSetting{
				Initial:       concurrency.InitialLimit,
				Max:           concurrency.MaxLimit,
				Min:           concurrency.MinLimit,
				BackoffFactor: limiter.DefaultBackoffFactor,
			})
		} else {
			perRPCLimits[concurrency.RPC] = limiter.NewAdaptiveLimit(limitName, limiter.AdaptiveSetting{
				Initial: concurrency.Concurrency(),
			})
		}

		// Create unauthenticated limiter if configured
		unauthLimits := concurrency.Unauthenticated
		if unauthLimits.IsSet() {
			limitNameUnauth := fmt.Sprintf("perRPC%s-unauthenticated", concurrency.RPC)
			if unauthLimits.Adaptive {
				perRPCLimitsUnauthenticated[concurrency.RPC] = limiter.NewAdaptiveLimit(limitNameUnauth, limiter.AdaptiveSetting{
					Initial:       unauthLimits.InitialLimit,
					Max:           unauthLimits.MaxLimit,
					Min:           unauthLimits.MinLimit,
					BackoffFactor: limiter.DefaultBackoffFactor,
				})
			} else if unauthLimits.Concurrency() > 0 {
				perRPCLimitsUnauthenticated[concurrency.RPC] = limiter.NewAdaptiveLimit(limitNameUnauth, limiter.AdaptiveSetting{
					Initial: unauthLimits.Concurrency(),
				})
			}
		}
	}
```
