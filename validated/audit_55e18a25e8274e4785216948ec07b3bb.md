### Title
Concurrency Limiting Is Applied Only After Full gRPC Message Deserialization, and Most RPCs Are Unprotected by Default - ([File: internal/grpc/middleware/limithandler/middleware.go])

### Summary
Gitaly's request-limiting mechanism (`LimiterMiddleware`) mirrors the reported JSON-RPC flaw: message deserialization happens before any concurrency/rate control is applied, and the control itself only exists for RPCs that an operator has explicitly opted into via `[[concurrency]]` configuration. By default, unconfigured RPCs pass straight through with no limiting at all.

### Finding Description
In gRPC, the unary/stream framework fully unmarshals the incoming protobuf message into `req interface{}` (or via `stream.RecvMsg`) *before* invoking the interceptor chain. Gitaly's `LimiterMiddleware.UnaryInterceptor` and `StreamInterceptor` receive the already-parsed request and only then look up a per-method limiter: [1](#0-0) 

If no `lockKey` can be derived, or if no limiter map entry exists for `info.FullMethod`, the request bypasses limiting entirely and goes straight to the handler: [2](#0-1) 

Limiters are only created for RPCs explicitly listed in the `[[concurrency]]` config array; everything else has `methodLimiters[method] == nil`: [3](#0-2) 

This is documented as intentional in `doc/backpressure.md`, which explicitly states rate limiting was removed from Gitaly and only concurrency queuing per explicitly-configured RPC remains: [4](#0-3) 

The limit-handler interceptors are wired into the server's interceptor chain, but they run *after* gRPC's transport layer has already deserialized the message (this is inherent to grpc-go's `processUnaryRPC`/`processStreamingRPC`, which decode the payload before calling into `ChainUnaryInterceptor`/`ChainStreamInterceptor`): [5](#0-4) 

Compounding this, `internal/gitaly/server/server.go` does not set `grpc.MaxRecvMsgSize` on the server, so grpc-go's built-in default caps each individual message, but does not bound the number of messages an ordinary authenticated client can send in a stream (e.g. `SSHReceivePack`, `PostReceivePack`, `PreReceiveHook`, `ProcReceiveHook`), each of which is buffered/copied server-side without any concurrency gate unless the specific RPC has been added to `[[concurrency]]`: [6](#0-5) [7](#0-6) 

### Impact Explanation
Any ordinary authenticated user (push/fetch/hook-triggering client) can invoke any RPC that isn't explicitly present in the operator's `[[concurrency]]` list — which by default is empty/minimal (only `ReplicateRepository` gets a hardcoded fallback limiter) — with unbounded concurrency and no queueing backpressure. Because the protobuf message is already fully parsed into memory before the limiter is even consulted, and streaming RPCs allow arbitrarily many messages/bytes to be pumped into git subprocess pipes, a client can drive many concurrent, large, unthrottled requests against storage/git-command-invoking RPCs, leading to memory and CPU exhaustion on the Gitaly node — a direct DoS of the RPC handler, structurally analogous to the reported "parsing before rate limiting" + "coverage gap" issue.

### Likelihood Explanation
High likelihood for any Gitaly deployment relying on default or partial `[[concurrency]]` configuration (which the shipped `config.toml.example` shows as commented-out/opt-in), since exploitation requires nothing more than a normal client issuing ordinary Git operations (clone/push) at high concurrency against RPCs not covered by an explicit limiter entry — no privileged access, leaked token, or malicious peer required.

### Recommendation
1. Apply a default/global concurrency and per-connection resource ceiling that covers *all* RPCs, not just those explicitly enumerated in `[[concurrency]]`, so unconfigured methods are not left completely unprotected.
2. Set explicit `grpc.MaxRecvMsgSize`/message-count and stream-duration bounds independent of per-RPC opt-in configuration.
3. Consider a lightweight pre-dispatch admission control (e.g., a global inflight-bytes/connection semaphore) that operates before/alongside deserialization for streaming RPCs, rather than relying solely on the optional per-RPC `ConcurrencyLimiter`.

### Proof of Concept
1. Deploy Gitaly with a `config.toml` that has no (or a partial) `[[concurrency]]` section, as shown in the shipped example (commented out by default).
2. As an ordinary authenticated client, open many concurrent streams to an RPC not present in the `[[concurrency]]` list (e.g. `SSHReceivePack`, `PreReceiveHook`, or any accessor RPC), sending large payload chunks per stream.
3. Observe that `LimiterMiddleware.UnaryInterceptor`/`StreamInterceptor` return `handler(ctx, req)` unmodified for every one of these calls because `methodLimiters[fullMethod]` is `nil`, allowing unrestricted concurrent memory/CPU consumption on the server.

### Citations

**File:** internal/grpc/middleware/limithandler/middleware.go (L75-98)
```go
func (c *LimiterMiddleware) UnaryInterceptor() grpc.UnaryServerInterceptor {
	return func(ctx context.Context, req interface{}, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (interface{}, error) {
		lockKey := c.getLockKey(ctx)
		if lockKey == "" {
			return handler(ctx, req)
		}

		// Check if request is authenticated
		limiter := c.methodLimiters[info.FullMethod]
		unauthLimiter, ok := c.methodLimitersUnauthenticated[info.FullMethod]
		if !auth.IsAuthenticated(ctx) && ok {
			limiter = unauthLimiter
		}

		if limiter == nil {
			// No concurrency limiting
			return handler(ctx, req)
		}

		return limiter.Limit(ctx, lockKey, func() (interface{}, error) {
			return handler(ctx, req)
		})
	}
}
```

**File:** internal/grpc/middleware/limithandler/middleware.go (L174-211)
```go
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

**File:** doc/backpressure.md (L13-56)
```markdown
We employ concurrency limiting as our primary backpressure mechanism in Gitaly.

## Concurrency queue

Limit the number of concurrent RPCs that are in flight on each Gitaly node for each
repository per RPC using `[[concurrency]]` configuration:

```toml
[[concurrency]]
rpc = "/gitaly.SmartHTTPService/PostUploadPackWithSidechannel"
max_per_repo = 1
```

For example:

- One clone request comes in for repository "A" (a largish repository).
- While this RPC is executing, another request comes in for repository "A". Because
  `max_per_repo` is 1 in this case, the second request blocks until the first request
  is finished.

An in-memory queue of requests can build up in Gitaly that are waiting their turn. Because
this is a potential vector for a memory leak, two other values in the `[[concurrency]]`
configuration can prevent an unbounded in-memory queue of requests:

- `max_queue_wait` is the maximum amount of time a request can wait in the
  concurrency queue. When a request waits longer than this time, it returns
  an error to the client.
- `max_queue_size` is the maximum size the concurrency queue can grow for a
  given RPC. If a concurrency queue is at its maximum, subsequent requests
  return with an error. The queue size is per repository.

For example:

```toml
[[concurrency]]
rpc = "/gitaly.SmartHTTPService/PostUploadPackWithSidechannel"
max_per_repo = 1
max_queue_wait = "1m"
max_queue_size = 5
```

## Note on Rate Limiting

Rate limiting has been removed from Gitaly. For more information about why and the alternatives, please see [issue #5011](https://gitlab.com/gitlab-org/gitaly/-/issues/5011).
```

**File:** internal/gitaly/server/server.go (L151-171)
```go
	unaryServerInterceptors := []grpc.UnaryServerInterceptor{
		grpccorrelation.UnaryServerCorrelationInterceptor(), // Must be above the metadata handler
		requestinfohandler.UnaryInterceptor,
		grpcprometheus.UnaryServerInterceptor,
		customfieldshandler.UnaryInterceptor,
		selector.UnaryServerInterceptor(s.logger.WithField("component", "gitaly.UnaryServerInterceptor").UnaryServerInterceptor(
			grpcmwlogrus.WithTimestampFormat(gitalylog.LogTimestampFormat),
			logMsgProducer,
			grpcmwlogrus.WithLevels(levelFunc),
		), logMatcher),
		loghandler.UnaryLogDataCatcherServerInterceptor(),
		sentryhandler.UnaryLogHandler(),
		statushandler.AbortedErrorUnaryInterceptor,
		statushandler.Unary, // Should be below LogHandler and above AbortedInterceptor in case this returns Aborted in the future
		auth.UnaryServerInterceptor(s.cfg.Auth),
	}
	// Should be below auth handler to prevent v2 hmac tokens from timing out while queued
	for _, limitHandler := range s.limitHandlers {
		streamServerInterceptors = append(streamServerInterceptors, limitHandler.StreamInterceptor())
		unaryServerInterceptors = append(unaryServerInterceptors, limitHandler.UnaryInterceptor())
	}
```

**File:** internal/gitaly/service/ssh/receive_pack.go (L69-94)
```go
func (s *server) sshReceivePack(stream gitalypb.SSHService_SSHReceivePackServer, req *gitalypb.SSHReceivePackRequest) (returnedErr error) {
	ctx := stream.Context()

	stdin := streamio.NewReader(func() ([]byte, error) {
		request, err := stream.Recv()
		return request.GetStdin(), err
	})

	var m sync.Mutex
	stdout := streamio.NewSyncWriter(&m, func(p []byte) error {
		return stream.Send(&gitalypb.SSHReceivePackResponse{Stdout: p})
	})

	// We both need to listen in on the stderr stream in order to be able to judge what exactly
	// is happening, but also relay the output to the client. We thus create a MultiWriter to
	// enable both at the same time.
	var stderrBuilder strings.Builder
	stderr := streamio.NewSyncWriter(&m, func(p []byte) error {
		return stream.Send(&gitalypb.SSHReceivePackResponse{Stderr: p})
	})
	stderr = io.MultiWriter(&stderrBuilder, stderr)

	repoPath, err := s.locator.GetRepoPath(ctx, req.GetRepository())
	if err != nil {
		return err
	}
```

**File:** internal/gitaly/service/smarthttp/receive_pack.go (L69-92)
```go
func (s *server) postReceivePack(
	stream gitalypb.SmartHTTPService_PostReceivePackServer,
	req *gitalypb.PostReceivePackRequest,
) (returnedErr error) {
	ctx := stream.Context()

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
```
