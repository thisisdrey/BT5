### Title
Per-repository (not per-user) concurrency queue lets any repo-accessing client cheaply exhaust the request queue and DoS legitimate fetches/pushes to that repository - (File: `internal/grpc/middleware/limithandler/middleware.go`)

### Summary
Gitaly's only defense against request floods is concurrency limiting; explicit rate limiting was intentionally removed [1](#0-0) . The concurrency/queue limiter keys admission solely on the RPC name plus the target repository's relative path via `LimitConcurrencyByRepo`, with no per-user/per-client component [2](#0-1) . This mirrors the Well AMM bug class: there is no "fee" (per-caller cost) attached to consuming a scarce shared resource (the per-repo queue slot), so any client that can reach the RPC for a given repository can cheaply saturate that repository's shared queue and lock out legitimate users, at negligible cost to the attacker relative to the disruption caused to victims.

### Finding Description
For RPCs configured with `[[concurrency]]` limits (e.g. `PostUploadPackWithSidechannel`), Gitaly enforces `max_per_repo` concurrent executions and a bounded `max_queue_size`/`max_queue_wait` queue *per repository, per RPC* [3](#0-2) . The lock/limiting key used by the default handler setup is `info.Repository.GetRelativePath()` only — it does not incorporate the caller's identity [2](#0-1) , and it's this key that's passed straight into the `ConcurrencyLimiter` [4](#0-3) .

Admission into the queue is cheap: `acquire()` first does a non-blocking `TryAcquire()` on the queue-token semaphore, and only after successfully entering the queue does it try to obtain a concurrency token (potentially blocking up to `maxQueueWait`) [5](#0-4) . Because there is no fee/cost/backoff applied per distinct caller (rate limiting was removed, per `doc/backpressure.md`), any actor that is authorized to read/write a given repository (e.g., a Reporter-level user with clone access, or any principal holding a valid Gitaly auth token used by GitLab-shell/Workhorse on behalf of many end users) can:

1. Open `max_per_repo` concurrent long-running/slow-draining requests against that repository's RPC (occupying all concurrency tokens), and
2. Fill the remaining `max_queue_size` queue slots with additional requests that just sit until `max_queue_wait` expires.

Because both the concurrency and queue keys are scoped only to `(RPC, repository)` and not to the caller, all subsequent legitimate callers for that same repository — including the original victim — are immediately rejected with `RESOURCE_EXHAUSTED`/`LimitError` once the queue is full [6](#0-5) , exactly as demonstrated in the test harness that shows a burst of same-key requests collapsing into "maximum queue size reached" errors for everyone sharing that key [7](#0-6) .

This is the direct analog of the Well finding: the AMM had zero swap fee, making it "too cheap" to repeatedly disrupt legitimate swaps; Gitaly similarly imposes no differentiated cost for repeatedly consuming a shared per-repository admission resource, since rate limiting has been explicitly removed and the queue key carries no per-caller weighting or backoff/penalty for the offending caller.

### Impact Explanation
An attacker with ordinary (non-privileged) read/fetch access to a single repository can deny legitimate clone/fetch/push operations on that repository for as long as they keep the queue and concurrency slots occupied, at a cost proportional only to opening a handful of connections — far cheaper than the disruption imposed on all other users of that repository (analogous to "long-term denial of service due to lack of fees" in the source report). Because the limiting key has no notion of caller identity, there's no mechanism (short of external rate limiting at Workhorse/Rails, which Gitaly itself does not implement per `doc/backpressure.md`) to distinguish the attacker's cheap flood from legitimate traffic and penalize only the attacker.

### Likelihood Explanation
Any client capable of issuing repeated RPCs for a given repository (clone/fetch access is often broadly granted, e.g., to CI runners, Reporters, or public/internal projects) can trigger this without special privileges, valid write access, or exploiting a memory-safety bug — it only requires knowledge of the target repository and the ability to open several connections, which is trivial and repeatable indefinitely since there's no per-caller rate limiting or cost imposed by Gitaly.

### Recommendation
Incorporate a per-caller (or per-authenticated-principal / per-source-IP) dimension into the concurrency/queue limiting key so that a single caller cannot unilaterally consume the entire per-repository queue budget, and/or apply differentiated backoff/penalties to callers that repeatedly queue-and-abandon or repeatedly hit `max_queue_size`/`max_queue_wait`. Consider reintroducing a lightweight cost-aware admission signal (as sketched for `streamcache` cost-aware admission) that accounts for caller identity, not just repository+RPC, and metering to detect and throttle callers responsible for disproportionate queue churn.

### Proof of Concept
1. Configure `[[concurrency]] rpc = "/gitaly.SmartHTTPService/PostUploadPackWithSidechannel" max_per_repo = 1 max_queue_wait = "1m" max_queue_size = 5` on a target repo, as documented [8](#0-7) .
2. As an attacker holding only fetch access to the repository, open 1 long-running fetch (occupies the sole concurrency token) plus 5 additional fetch requests that never complete quickly (fills `max_queue_size`).
3. A legitimate user's fetch to the same repository, using a different account/IP, is immediately rejected with `LimitError: "maximum queue size reached"`, mirroring the rejection behavior demonstrated in `TestConcurrencyLimitHandlerMetrics` [9](#0-8) , because the limiting key (`repository relative path`) is shared by attacker and victim alike [2](#0-1) .
4. The attacker repeats this indefinitely at negligible cost since no per-caller rate limiting exists in Gitaly [1](#0-0) , sustaining denial of service for that repository.

### Citations

**File:** doc/backpressure.md (L15-52)
```markdown
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
```

**File:** doc/backpressure.md (L54-56)
```markdown
## Note on Rate Limiting

Rate limiting has been removed from Gitaly. For more information about why and the alternatives, please see [issue #5011](https://gitlab.com/gitlab-org/gitaly/-/issues/5011).
```

**File:** internal/grpc/middleware/limithandler/middleware.go (L18-25)
```go
// LimitConcurrencyByRepo implements GetLockKey by using the repository path as lock.
func LimitConcurrencyByRepo(ctx context.Context) string {
	if info := requestinfohandler.Extract(ctx); info != nil {
		return info.Repository.GetRelativePath()
	}

	return ""
}
```

**File:** internal/grpc/middleware/limithandler/middleware.go (L74-97)
```go
// UnaryInterceptor returns a Unary Interceptor
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
```

**File:** internal/limiter/concurrency_limiter.go (L60-107)
```go
// acquire tries to acquire the semaphore. It may fail if the admission queue is full or if the max
// queue-time ticker ticks before acquiring a concurrency token.
func (sem *keyedConcurrencyLimiter) acquire(ctx context.Context, limitingKey string) (returnedErr error) {
	if sem.queueTokens != nil {
		// Try to acquire the queueing token. The queueing token is used to control how many
		// callers may wait for the concurrency token at the same time. If there are no more
		// queueing tokens then this indicates that the queue is full and we thus return an
		// error immediately.
		if err := sem.queueTokens.TryAcquire(); err != nil {
			return err
		}
		// We have acquired a queueing token, so we need to release it if acquiring
		// the concurrency token fails. If we succeed to acquire the concurrency
		// token though then we retain the queueing token until the caller signals
		// that the concurrency-limited function has finished. As a consequence the
		// queue token is returned together with the concurrency token.
		//
		// A simpler model would be to just have `maxQueueLength` many queueing
		// tokens. But this would add concurrency-limiting when acquiring the queue
		// token itself, which is not what we want to do. Instead, we want to admit
		// as many callers into the queue as the queue length permits plus the
		// number of available concurrency tokens allows.
		defer func() {
			if returnedErr != nil {
				sem.queueTokens.Release()
			}
		}()
	}

	// We are queued now, so let's tell the monitor. Furthermore, even though we're still
	// holding the queueing token when this function exits successfully we also tell the monitor
	// that we have exited the queue. It is only an implementation detail anyway that we hold on
	// to the token, so the monitor shouldn't care about that.
	sem.monitor.Queued(ctx, limitingKey, sem.queueLength())
	defer sem.monitor.Dequeued(ctx)

	if sem.maxQueueWait != 0 {
		if sem.setWaitTimeoutContext != nil {
			ctx = sem.setWaitTimeoutContext()
		} else {
			var cancel context.CancelFunc
			ctx, cancel = context.WithTimeout(ctx, sem.maxQueueWait)
			defer cancel()
		}
	}

	// Try to acquire the concurrency token now that we're in the queue.
	return sem.concurrencyTokens.Acquire(ctx)
```

**File:** internal/limiter/concurrency_limiter.go (L197-234)
```go
func (c *ConcurrencyLimiter) Limit(ctx context.Context, limitingKey string, f LimitedFunc) (interface{}, error) {
	span, ctx := tracing.StartSpanIfHasParent(
		ctx,
		"limiter.ConcurrencyLimiter.Limit",
		[]attribute.KeyValue{
			attribute.String("key", limitingKey),
		},
	)
	defer span.End()

	if c.currentLimit() <= 0 {
		return f()
	}

	sem := c.getConcurrencyLimit(limitingKey)
	defer c.putConcurrencyLimit(limitingKey)

	start := time.Now()

	if err := sem.acquire(ctx, limitingKey); err != nil {
		queueTime := time.Since(start)
		switch {
		case errors.Is(err, ErrMaxQueueSize):
			c.monitor.Dropped(ctx, limitingKey, sem.queueLength(), sem.inProgress(), queueTime, "max_size")
			return nil, structerr.NewResourceExhausted("%w", ErrMaxQueueSize).WithDetail(&gitalypb.LimitError{
				ErrorMessage: err.Error(),
				RetryAfter:   durationpb.New(0),
			})
		case errors.Is(err, ErrMaxQueueTime):
			c.monitor.Dropped(ctx, limitingKey, sem.queueLength(), sem.inProgress(), queueTime, "max_time")
			return nil, structerr.NewResourceExhausted("%w", ErrMaxQueueTime).WithDetail(&gitalypb.LimitError{
				ErrorMessage: err.Error(),
				RetryAfter:   durationpb.New(0),
			})
		default:
			c.monitor.Dropped(ctx, limitingKey, sem.queueLength(), sem.inProgress(), queueTime, "other")
			return nil, fmt.Errorf("unexpected error when dequeueing request: %w", err)
		}
```

**File:** internal/grpc/middleware/limithandler/middleware_test.go (L733-799)
```go
func TestConcurrencyLimitHandlerMetrics(t *testing.T) {
	s := &queueTestServer{reqArrivedCh: make(chan struct{})}
	s.blockCh = make(chan struct{})

	methodName := "/grpc.testing.TestService/UnaryCall"
	cfg := config.Cfg{
		Concurrency: []config.Concurrency{
			{
				RPC: methodName,
				ConcurrencyLimits: config.ConcurrencyLimits{
					MaxPerRepo:   1,
					MaxQueueSize: 1,
				},
			},
		},
	}

	_, _, setupPerRPCConcurrencyLimiters := limithandler.WithConcurrencyLimiters(cfg)
	lh := limithandler.New(cfg, fixedLockKey, setupPerRPCConcurrencyLimiters)
	interceptor := lh.UnaryInterceptor()
	srv, serverSocketPath := runServer(t, s, grpc.UnaryInterceptor(interceptor))
	defer srv.Stop()

	client, conn := newClient(t, serverSocketPath)
	defer conn.Close()
	ctx := testhelper.Context(t)
	respCh := make(chan *grpc_testing.SimpleResponse)
	go func() {
		resp, err := client.UnaryCall(ctx, &grpc_testing.SimpleRequest{})
		respCh <- resp
		require.NoError(t, err)
	}()
	// wait until the first request is being processed. After this, requests will be queued
	<-s.reqArrivedCh

	errChan := make(chan error)
	// out of ten requests, the first one will be queued and the other 9 will return with
	// an error
	for i := 0; i < 10; i++ {
		go func() {
			resp, err := client.UnaryCall(ctx, &grpc_testing.SimpleRequest{})
			if err != nil {
				errChan <- err
			} else {
				respCh <- resp
			}
		}()
	}

	var errs int
	for err := range errChan {
		s, ok := status.FromError(err)
		require.True(t, ok)
		details := s.Details()
		require.Len(t, details, 1)

		limitErr, ok := details[0].(*gitalypb.LimitError)
		require.True(t, ok)

		assert.Equal(t, limiter.ErrMaxQueueSize.Error(), limitErr.GetErrorMessage())
		assert.Equal(t, durationpb.New(0), limitErr.GetRetryAfter())

		errs++
		if errs == 9 {
			break
		}
	}
```
