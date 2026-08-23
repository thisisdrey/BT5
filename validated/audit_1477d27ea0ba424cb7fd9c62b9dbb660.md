Found it — this is the concrete analog. The `doc/load-management-architecture.md` describes a "Cost-Aware Admission" layer that sits **before** the concurrency limiter, and requests served from `streamcache` (pack-objects cache hits) **bypass the concurrency limiter entirely**, going straight to the handler. This directly mirrors the mempool bug's structure: an entire class of requests is inserted/processed through a path that never goes through the capacity-enforcing check (`canAddTx`/`ConcurrencyLimiter.Limit`), so it can grow unbounded and starve/flood the system while the "normal" admission path still enforces its limit.

However, to validate this is an exploitable DoS (not just a documented design tradeoff), I need to confirm exactly how the cache-hit bypass is wired into the `PostUploadPackWithSidechannel` handler and whether an attacker can force cheap/attacker-controlled "cache hit" traffic to flood the server unbounded, since `min_occurrences` and the streamcache's own size aren't a substitute for the removed concurrency limiter.### Title
Cost-Aware Admission bypass allows unbounded concurrency for cache-servable fetch/clone requests, defeating the Concurrency Limiter - (File: doc/load-management-architecture.md)

### Summary
The mempool report describes a class of transactions (`pendingTxs`, future-nonce) that are inserted through a code path that never calls `canAddTx`, the function that enforces the configured capacity limit — allowing that class to grow unboundedly and flood the system, even though the "normal" insertion path is correctly bounded. Gitaly's own load-management design contains a structurally identical split-admission-path pattern: the "Cost-Aware Admission" layer explicitly routes `streamcache`/pack-objects-cache-servable requests around the `ConcurrencyLimiter` entirely.

### Finding Description
`doc/load-management-architecture.md` documents that a "Cost-Aware Admission" layer sits in front of the gRPC interceptor chain, before the `ConcurrencyLimiter`: [1](#0-0) 

For `PostUploadPackWithSidechannel` (clone/fetch) requests, if the request "can be served from `streamcache`" (i.e. the pack-objects cache), it "bypasses the concurrency limiter entirely and proceeds directly to the handler." [2](#0-1) 

This is the same bug class as the mempool report: the capacity-enforcing check (`canAddTx` in the report; `ConcurrencyLimiter.Limit`/`sem.acquire` in Gitaly, which is the only place that enforces `max_per_repo`/`max_concurrency` and `max_queue_size`) is skipped for a whole category of requests. In Gitaly's `ConcurrencyLimiter`, admission is strictly gated by `sem.acquire`, which tries to obtain a queue token and then a concurrency token, rejecting the caller with `ErrMaxQueueSize`/`ErrMaxQueueTime` when capacity is exhausted: [3](#0-2)  and this is invoked from the gRPC middleware's `UnaryInterceptor`/`RecvMsg`, which is the sole enforcement point for per-repo/per-RPC concurrency: [4](#0-3) 

The design doc states the rationale is that cache-hit requests are "cheap" (disk streaming) versus cache-miss requests that run full git subprocess pipelines, so cache hits "skip the limiter entirely." [5](#0-4) 

### Impact Explanation
If an ordinary, unprivileged client can trigger or predict a pack-objects cache hit (e.g., by repeatedly fetching the same ref/pack from a public or accessible repository, matching `computeCacheKey`-derived criteria used elsewhere in the codebase for `PackObjectsHookWithSidechannelRequest`), such requests entirely skip the `[[concurrency]] max_per_repo` / `max_queue_size` protections that are Gitaly's primary DoS backpressure mechanism (as documented in `doc/backpressure.md`). [6](#0-5)  An attacker could fire an unbounded number of parallel clone/fetch requests that hit the cache, each consuming a goroutine, gRPC stream, socket/sidechannel resources, and disk-streaming I/O, without ever being throttled or queued — unlike normal cache-miss traffic which is properly bounded. This mirrors the mempool report's impact: an entire class of "cheap-looking" work bypasses the resource cap and can flood the node, potentially exhausting connections/goroutines/I/O bandwidth and degrading or halting service for legitimate clients (analogous to halting block production in the original report).

### Likelihood Explanation
Likelihood is moderate-to-uncertain. The design document positions the bypass as a considered, current architectural decision (not accidental), and the actual gRPC-interceptor-level code for this "Cost-Aware Admission" layer was not found in the indexed source (`internal/gitaly/service/smarthttp/*.go` for `PostUploadPackWithSidechannel` did not show a distinct cache-hit fast path in the retrieved snippets, only `internal/gitaly/service/smarthttp/cache.go` had a single unrelated match). I could not fully confirm from the available index (1) whether this bypass is already implemented in code versus only planned/documented, (2) the exact predicate used to decide "can be served from streamcache" before the handler executes (and whether it is attacker-triggerable pre-admission, since a cache lookup typically requires already running part of the request), and (3) whether other backstops (e.g., PackObjectsLimiting concurrency limiter on `PackObjectsHookWithSidechannel`, separate from the top-level RPC concurrency limiter) still apply to these bypassed requests. Given the index size limits, some file contents may not be available — a full code review (not just the design doc) would be needed to confirm implementation status and any compensating controls.

### Recommendation
- Ensure cache-servable requests still consume a (possibly cheaper-weighted) admission slot rather than bypassing the `ConcurrencyLimiter` entirely — e.g., via a cost-weighted token instead of a full bypass.
- Add an independent, hard cap (analogous to `max_queue_size`) on the number of concurrent bypassed/cache-hit requests per repository/RPC, so this path cannot grow unboundedly even when each individual request is cheap.
- Verify whether `PackObjectsLimiting` (the separate limiter for `PackObjectsHookWithSidechannel`) still applies when this cost-aware admission bypass is exercised, and if not, treat this as the same root-cause gap as the mempool bug: a secondary insertion/admission path that isn't covered by the primary capacity check.
- Document and test the interaction between Cost-Aware Admission and `ConcurrencyLimiter` explicitly, with a concurrency test analogous to `TestConcurrencyLimiter_queueLimit` that verifies bypassed cache-hit traffic cannot exceed a bounded ceiling.

### Proof of Concept
Not independently verifiable from the indexed code — the only concrete evidence found is the architecture document explicitly describing the bypass:
```
Request -> Cost-Aware Admission -> Concurrency Limiter -> Handler
                 |
           (cache hit?)
                 |
           bypass limiter -> Handler directly
``` [7](#0-6) 
A conceptual PoC: an unauthenticated/low-privilege client repeatedly issues `PostUploadPackWithSidechannel` fetch requests against the same ref of an accessible repository so that most requests hit the pack-objects cache; per the documented design these requests bypass `max_per_repo`/`max_queue_size`, so the client can open far more concurrent fetch streams against that repository than the configured `[[concurrency]]` limit would normally allow, exhausting node resources reserved for other users' cache-miss traffic. Actual exploitability depends on implementation details not confirmed in this pass; a Devin session with full repository access would be required to locate the interceptor code implementing this bypass and confirm the exact trigger conditions.

### Citations

**File:** doc/load-management-architecture.md (L179-195)
```markdown
### Cost-Aware Admission

A separate admission layer that sits before the concurrency limiter in the
gRPC interceptor chain. If a request can be served from
[`streamcache`](../internal/streamcache/cache.go), it bypasses the concurrency
limiter entirely and proceeds directly to the handler. Currently this applies
to:

- [`PostUploadPackWithSidechannel`](../internal/gitaly/service/smarthttp/upload_pack.go) (clone/fetch)

```plaintext
Request -> Cost-Aware Admission -> Concurrency Limiter -> Handler
                 |
           (cache hit?)
                 |
           bypass limiter -> Handler directly
```
```

**File:** doc/load-management-architecture.md (L197-205)
```markdown
The concurrency limiter today treats every request as equally expensive. In
practice, requests served from cache consume minimal resources (disk streaming)
while cache misses run full Git subprocess pipelines. Rather than feeding the
limiter a cost hint (which still consumes a limiter slot), cache-hit requests
skip the limiter entirely — keeping the concurrency limiter pure and ensuring
limits designed for expensive operations do not penalize cheap ones.

This separation also keeps the concurrency limiter's responsibility narrow: every
request that reaches it is treated as expensive.
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

**File:** internal/grpc/middleware/limithandler/middleware.go (L74-98)
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
}
```

**File:** doc/backpressure.md (L9-24)
```markdown
If there is a surge of traffic beyond what Gitaly can handle, Gitaly should
be able to push back on the client calling. Gitaly shouldn't subserviently agree
to process more than it can handle.

We employ concurrency limiting as our primary backpressure mechanism in Gitaly.

## Concurrency queue

Limit the number of concurrent RPCs that are in flight on each Gitaly node for each
repository per RPC using `[[concurrency]]` configuration:

```toml
[[concurrency]]
rpc = "/gitaly.SmartHTTPService/PostUploadPackWithSidechannel"
max_per_repo = 1
```
```
