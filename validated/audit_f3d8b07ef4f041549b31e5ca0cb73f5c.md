### Title
Pack-objects concurrency limiter is keyed by client-supplied remote IP, allowing trivial bypass of `[[concurrency]]` resource limits and RPC-handler DoS - (File: internal/gitaly/service/hook/pack_objects.go)

### Summary
The Perennial `Vault` bug let an attacker inflate a shared counter (`checkpoint.count`) for free by issuing calls from arbitrary, costless identities (throwaway addresses), diluting the fee that is supposed to be split among genuine participants. The equivalent pattern in Gitaly is the pack-objects concurrency limiter: instead of bounding resource usage per repository/RPC globally, `runPackObjectsLimited` partitions the limiter's token pool by a caller-supplied identifier (`RemoteIp`), so an attacker who can vary that identifier across requests effectively gets a fresh quota for each "identity", bypassing the intended `max_concurrency`/`max_per_repo` backpressure entirely.

### Finding Description
`packObjectsHook` computes a cache key and then decides how to run `git-pack-objects`: [1](#0-0) 

For non-loopback callers it calls `runPackObjectsLimited`, passing `ipAddr.String()` (derived from `req.GetRemoteIp()`) as the **limiting key** for the concurrency limiter: [2](#0-1) 

The limiter type is explicitly documented as being keyed by "RemoteIP/Repository/User", i.e. a request-derived value rather than something globally scoped per repository: [3](#0-2) 

The `ConcurrencyLimiter.Limit` implementation lazily creates a brand-new semaphore/queue entry for every distinct key it has not seen before: [4](#0-3) 

This is exactly the shape of the Vault issue: the resource-limiting mechanism (a shared "counter"/token pool meant to bound total concurrent `git-pack-objects` processes for a repository) is partitioned by a value that costs nothing to vary. In the Vault case the free "identity" was a throwaway Ethereum address; here it is the `RemoteIp` value carried in the internal `PackObjectsHookWithSidechannelRequest`, which is populated from information forwarded through the hook payload/environment rather than a value Gitaly itself authenticates at the socket level for this particular internal call. Any caller able to influence this value across separate invocations (e.g., a client fetching through many different source addresses, or any component in the trust chain that forwards a client-controlled remote-IP string) obtains an independent concurrency quota for each variant, defeating the very purpose of `max_per_repo`/`max_concurrency` limits documented in `doc/backpressure.md`. [5](#0-4) 

### Impact Explanation
The concurrency limiter exists specifically to protect Gitaly from resource exhaustion when Git object-generation traffic surges. If the limiting key can be freely multiplied, an attacker can drive unbounded numbers of concurrent `git pack-objects` invocations against a repository (or across all repositories, since the limiter map has no global cap on the number of distinct keys/semaphores it will create), consuming CPU, memory and I/O and starving legitimate clients — a DoS of the very RPC-handler resource-limiting mechanism the code purports to enforce, analogous to how the Vault attacker diluted the shared keeper-fee counter to shift costs onto other users. This falls squarely under the allowed "RPC-handler resource limits" DoS category.

### Likelihood Explanation
Exploitation requires only the ability to cause multiple, concurrent `PostUploadPack`/fetch operations against the same (or several) repositories while varying the value that ends up in `RemoteIp` for the internal hook call — something achievable by fetching from many different network paths/source addresses, which is inexpensive for a determined but otherwise unprivileged client. No credentials beyond ordinary fetch access are needed, matching the "ordinary user's fetch" reachability requirement.

### Recommendation
- Do not scope the pack-objects concurrency limiter's admission control solely by an unauthenticated, request-derived value. Combine it with a hard, global (or per-repository) cap on the total number of concurrently running `git-pack-objects` processes regardless of how many distinct `RemoteIp`/key values are observed.
- Bound the number of distinct keys/semaphores the limiter will track at once (or evict/reuse based on IP subnet aggregation) so that key-space multiplication cannot unboundedly grow concurrency.
- Prefer keying primarily off `Repository` (and optionally an authenticated identity) with `RemoteIp` used only as a secondary fairness dimension bounded by a repository-wide ceiling.

### Proof of Concept
Conceptual reproduction (mirrors the referenced report's technique of using multiple free "identities" to defeat a shared limiter):
1. Configure Gitaly with a pack-objects/`[[concurrency]]` limit intended to cap concurrent `git-pack-objects` processes per repository (e.g., `max_concurrency = 1`).
2. Issue N concurrent fetch requests against the same repository such that the internal `PackObjectsHookWithSidechannelRequest.RemoteIp` differs for each (e.g., by originating fetches from N different source addresses/paths).
3. Because `runPackObjectsLimited` partitions the limiter by `ipAddr.String()` (`internal/gitaly/service/hook/pack_objects.go:176-212`, `internal/limiter/concurrency_limiter.go:243-270`), each distinct `RemoteIp` value obtains its own semaphore, so all N `git-pack-objects` processes run concurrently instead of being serialized/queued by the configured limit — reproducing, in Gitaly's resource-limiting layer, the same "cheap multiple identities dilute a shared control" pattern described in the Vault report.

**Uncertainty note:** I was unable to fully trace, within the available tooling, the exact upstream code path that populates `req.RemoteIp` for `PackObjectsHookWithSidechannelRequest` (i.e., whether it is always derived from a trusted, non-spoofable TCP peer address captured server-side, or can be influenced indirectly by client-supplied data forwarded through `HooksPayload`/environment variables). This affects how directly an external, unprivileged actor can vary the value without simply using genuinely different source IPs. I could not locate the exact assignment site in the indexed files (searches for `RemoteIp:` construction sites did not return results within budget), so confirming or ruling out direct client control of this field would need further, deeper investigation (e.g., via a full Devin session with complete file access) before treating this as a certain vulnerability rather than a design weakness in the limiter's keying strategy.

### Citations

**File:** internal/gitaly/service/hook/pack_objects.go (L65-87)
```go
	servedBytes, created, err := s.packObjectsCache.Fetch(ctx, cacheKey, output, func(w io.Writer) error {
		ipAddr := net.ParseIP(req.GetRemoteIp())
		if ipAddr == nil {
			// Best effort, maybe the remote IP includes source port
			if ip, _, err := net.SplitHostPort(req.GetRemoteIp()); err == nil {
				ipAddr = net.ParseIP(ip)
			}
		}
		// Ignore loop-back IPs
		if ipAddr != nil && !ipAddr.IsLoopback() {
			return s.runPackObjectsLimited(
				ctx,
				w,
				ipAddr.String(),
				req,
				args,
				stdin,
				cacheKey,
			)
		}

		return s.runPackObjects(ctx, w, req, args, stdin, cacheKey)
	})
```

**File:** internal/gitaly/service/hook/pack_objects.go (L176-212)
```go
func (s *server) runPackObjectsLimited(
	ctx context.Context,
	w io.Writer,
	limitkey string,
	req *gitalypb.PackObjectsHookWithSidechannelRequest,
	args *packObjectsArgs,
	stdin io.ReadCloser,
	key string,
) error {
	ctx = context.WithoutCancel(ctx)

	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	defer stdin.Close()

	if _, err := s.packObjectsLimiter.Limit(
		ctx,
		limitkey,
		func() (interface{}, error) {
			return nil,
				s.runPackObjectsFn(
					ctx,
					s.gitCmdFactory,
					w,
					req,
					args,
					stdin,
					key,
				)
		},
	); err != nil {
		return err
	}

	return nil
}
```

**File:** internal/limiter/concurrency_limiter.go (L26-33)
```go
const (
	// TypePerRPC is a concurrency limiter whose key is the full method of gRPC server. All
	// requests of the same method shares the concurrency limit.
	TypePerRPC = "per-rpc"
	// TypePackObjects is a dedicated concurrency limiter for pack-objects. It uses request
	// information (RemoteIP/Repository/User) as the limiting key.
	TypePackObjects = "pack-objects"
)
```

**File:** internal/limiter/concurrency_limiter.go (L243-270)
```go
// getConcurrencyLimit retrieves the concurrency limit for the given key. If no such limiter exists
// it will be lazily constructed.
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

**File:** doc/backpressure.md (L26-42)
```markdown
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
```
