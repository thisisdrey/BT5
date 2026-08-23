### Title
DoS of legitimate repository operations via shared per-repository concurrency queue exhaustion - ([File: internal/limiter/concurrency_limiter.go])

### Summary
The external report describes a griefing DoS in `vePeg.sol` where a `MAX_DELEGATES` capacity check is a shared, hardcoded resource that any unprivileged actor can cheaply fill (1 wei), permanently blocking a legitimate user's ability to perform a state-changing operation for a long period. The closest reachable analog in this Gitaly codebase is the gRPC concurrency-limiter's per-repository request queue: the queue capacity (`MaxQueueSize`/`maxQueueLength`) is a single shared resource keyed only by repository (and RPC), with no per-caller isolation, so any unprivileged client with RPC access to a given repository can fill the queue and cause `ResourceExhausted` rejections for other legitimate callers of that repository/RPC.

### Finding Description
`LimitConcurrencyByRepo` derives the limiter's `lockKey` purely from the target repository's relative path [1](#0-0) . That key is used to look up (or lazily create) a single `keyedConcurrencyLimiter` shared by *all* callers of the same RPC against the same repository [2](#0-1) .

Each keyed limiter has a fixed-size `queueTokens` semaphore sized to `currentLimit() + maxQueueLength`, i.e. a hardcoded/configured capacity analogous to `MAX_DELEGATES` [3](#0-2) . When a request arrives, it tries to acquire a queue token; if the queue is already full (because other requests — potentially attacker-issued, low-cost, unauthenticated-eligible requests — are occupying it) the request is rejected immediately with `ErrMaxQueueSize`, regardless of who is trying to enter [4](#0-3) [5](#0-4) .

Because the key is only `{RPC, repository}` and not further scoped by caller/user, any client capable of invoking the rate-limited RPC against a given repository (e.g. `PostUploadPackWithSidechannel` used for clone/fetch, per the documented example) can flood that RPC on that repository to occupy every queue slot, exactly mirroring the vePeg bug class: a shared fixed-capacity structure that combines the attacker's cheap "filler" entries with the victim's legitimate entry count, causing the victim's legitimate request to be rejected outright [6](#0-5) . Unlike the vePeg bug (which had a bounded time window of 52 epochs), an attacker here can sustain the flood indefinitely by continually re-issuing requests, extending the DoS for as long as they choose.

### Impact Explanation
Legitimate clone/fetch/push (or whichever RPC has a `[[concurrency]]` limit configured) requests against a targeted repository can be denied service (`ResourceExhausted`, `maximum queue size reached`) by any actor who can reach the RPC endpoint for that repository, without needing elevated privileges, a valid write token, or any git object manipulation — only enough requests to saturate the small, fixed queue capacity. This directly denies availability to real users of that repository, similar in kind to the vePeg finding where a legitimate user was denied the ability to perform a needed state change for an extended period.

### Likelihood Explanation
Likelihood is high in environments where `[[concurrency]]` limits are configured with a small `max_queue_size` (a documented, encouraged configuration in `doc/backpressure.md`) and where the rate-limited RPC is reachable by many different, low-trust clients per repository (e.g., a public repository's clone/fetch RPCs). No special timing (back-run) is even required — the attacker simply issues enough concurrent/queued requests to exceed `max_queue_size` for the repository/RPC pair being protected.

### Recommendation
Scope the concurrency/queue key more granularly than `{RPC, repository}` alone — for example, incorporate a per-caller/per-IP/per-token dimension (similar to how `TypePackObjects` already scopes by `RemoteIP/Repository/User`) so that no single unauthenticated or low-trust caller can consume the entire shared queue capacity intended to protect the repository for all its legitimate users. Alternatively, apply fairness/quota mechanisms (e.g., per-caller sub-limits within the per-repository queue) so a flood from one caller cannot starve queue slots from others.

### Proof of Concept
1. Configure Gitaly with a `[[concurrency]]` limit for a clone/fetch RPC (e.g. `PostUploadPackWithSidechannel`) with `max_per_repo = N` and a small `max_queue_size = M`, as shown in the documented example [7](#0-6) .
2. An unprivileged/low-trust client (any actor able to reach that RPC for the targeted repository, e.g. anonymous clone on a public repo) issues `N + M` concurrent long-running requests against the same repository, filling both the concurrency slots and the queue tokens (`getConcurrencyLimit`/`acquire` in `internal/limiter/concurrency_limiter.go`) [2](#0-1) .
3. A legitimate user's subsequent request for the same repository/RPC calls `sem.acquire`, fails `queueTokens.TryAcquire()`, and is immediately rejected with `structerr.NewResourceExhausted(... ErrMaxQueueSize ...)` [5](#0-4) .
4. As long as the attacker keeps issuing requests to refill the queue as slots free up, the legitimate user is continuously denied service for that repository/RPC.

### Citations

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

**File:** internal/limiter/concurrency_limiter.go (L60-70)
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
```

**File:** internal/limiter/concurrency_limiter.go (L216-224)
```go
	if err := sem.acquire(ctx, limitingKey); err != nil {
		queueTime := time.Since(start)
		switch {
		case errors.Is(err, ErrMaxQueueSize):
			c.monitor.Dropped(ctx, limitingKey, sem.queueLength(), sem.inProgress(), queueTime, "max_size")
			return nil, structerr.NewResourceExhausted("%w", ErrMaxQueueSize).WithDetail(&gitalypb.LimitError{
				ErrorMessage: err.Error(),
				RetryAfter:   durationpb.New(0),
			})
```

**File:** internal/limiter/concurrency_limiter.go (L245-270)
```go
func (c *ConcurrencyLimiter) getConcurrencyLimit(limitingKey string) *keyedConcurrencyLimiter {
	c.m.Lock()
	defer c.m.Unlock()

	if c.limitsByKey[limitingKey] == nil {
		// Set up the queue tokens in case a maximum queue length was requested. As the
		// queue tokens are kept during the whole lifetime of the concurrency-limited
		// function we add the concurrency tokens to the number of available token.
		var queueTokens semaphorer
		if c.maxQueueLength > 0 {
			queueTokens = c.createSemaphore(uint(c.currentLimit() + c.maxQueueLength))
		}

		c.limitsByKey[limitingKey] = &keyedConcurrencyLimiter{
			monitor:               c.monitor,
			maxQueueWait:          c.maxQueueWait,
			setWaitTimeoutContext: c.SetWaitTimeoutContext,
			concurrencyTokens:     c.createSemaphore(uint(c.currentLimit())),
			queueTokens:           queueTokens,
		}
	}

	c.limitsByKey[limitingKey].refcount++

	return c.limitsByKey[limitingKey]
}
```

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
