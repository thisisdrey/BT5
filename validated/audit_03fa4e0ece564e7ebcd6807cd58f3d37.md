### Title
PackObjectsHook concurrency limiter is skipped entirely when the client-supplied `RemoteIp` is loopback or unparsable, enabling unbounded `git-pack-objects` spawning - ([File: internal/gitaly/service/hook/pack_objects.go])

### Summary
`internal/gitaly/service/hook/pack_objects.go` charges "cost" for `git-pack-objects` invocations via a per-IP `ConcurrencyLimiter`, but this limiter is bypassed completely whenever the `RemoteIp` field carried in the request cannot be parsed as a non-loopback IP address. This mirrors the reported MemoryGrow bug class: a resource-metering path (`pricing_v1`/memory pricer analog = `packObjectsLimiter`) is unconditionally cheap (effectively free) for a specific, attacker-reachable condition (fixed/absent memory analog = loopback/unparsable IP), letting an unprivileged caller repeatedly trigger the expensive operation without ever being throttled.

### Finding Description
`packObjectsHook` decides whether to run `git-pack-objects` through the concurrency-limited path (`runPackObjectsLimited`) or the unlimited path (`runPackObjects`) based solely on parsing `req.GetRemoteIp()`: [1](#0-0) 

If `net.ParseIP` fails outright, or the parsed address `IsLoopback()`, the code falls straight into `s.runPackObjects(...)` — the path with **no concurrency limiter at all** — instead of `s.runPackObjectsLimited(...)`, which is the only place `s.packObjectsLimiter.Limit(...)` is invoked: [2](#0-1) 

This is directly analogous to the MemoryGrow issue: the pricing/limiting logic assumes a "normal" case (a genuine external IP) is metered by the concurrency limiter, but a special, easily reachable case (RemoteIp empty, malformed, or loopback) silently falls through to the un-metered code path, exactly like MemoryGrow being priced at a flat `1 ink` for the case where the "pay" hook doesn't apply.

The `packObjectsCache.Fetch` call wrapping this logic only deduplicates *identical* cache keys (same repository, args, git protocol, and stdin content); it does not itself throttle the number of concurrent *distinct* `git-pack-objects` invocations. Distinct pack requests (e.g., different `want`/`have` sets in the negotiation, which is easy for a client to vary), each with a `RemoteIp` value that fails to parse or resolves to loopback, will each spawn a new unmetered `git-pack-objects` subprocess.

### Impact Explanation
Because the limiter is the only backpressure mechanism protecting Gitaly from resource exhaustion on `git-pack-objects` (per `doc/backpressure.md`'s stated purpose of concurrency limiting), an attacker able to make the server treat requests as loopback/unparsable can fan out an unbounded number of concurrent, CPU/memory/IO-heavy `git-pack-objects` processes for one or more repositories, exhausting Gitaly node resources — a direct denial-of-service against the RPC handler, consistent with the "DoS of a handler" acceptance criterion.

### Likelihood Explanation
The reachability of this bypass hinges on how easily `RemoteIp` can end up empty, malformed, or loopback in practice — for example, deployments/paths where `gitaly-hooks` invokes `PackObjectsHookWithSidechannel` without a populated/valid remote IP (e.g., local/unix-socket-adjacent flows, or any caller that fails to set the field), or environments where address parsing legitimately yields `127.0.0.1`/`::1` due to proxying. The code path is unconditionally reached from `PackObjectsHookWithSidechannel` for any ordinary git fetch, which is called as part of every `git-upload-pack` invocation. I was not able to fully trace, within the indexed context, the exact upstream code that populates `RemoteIp` in the `HooksPayload`/environment variables before it reaches `gitaly-hooks` — that would determine precisely how directly a remote, unprivileged client can force this value into an empty/loopback state. This should be validated with a background Devin session that can trace `GL_REMOTE_IP` propagation from `PostUploadPack`/`PostReceivePack` handlers through the hooks payload into `gitaly-hooks`.

### Recommendation
- **Short term:** Do not skip concurrency limiting when `RemoteIp` fails to parse or is loopback. Fall back to a stable limiting key (e.g., the repository path, or a constant "unknown" bucket) rather than bypassing `s.packObjectsLimiter` entirely.
- **Long term:** Audit all "fast path" / "trust" branches in resource-limiting code (this one, and the documented but seemingly unimplemented "Cost-Aware Admission" streamcache bypass mentioned in `doc/load-management-architecture.md` for `PostUploadPackWithSidechannel`) to ensure no attacker-influenceable condition can silently disable metering, and add fuzz/adversarial tests that specifically exercise edge-case metadata (empty, malformed, loopback, private-range IPs) against every concurrency-limiting decision point.

### Proof of Concept
1. Trigger a `git fetch`/clone against a Gitaly-backed repository through a path where the resulting `PackObjectsHookWithSidechannelRequest.RemoteIp` is empty, malformed, or resolves to a loopback address (this is the condition under test in `pack_objects_test.go`'s cache tests, which construct requests without setting `RemoteIp`, e.g. the "all requests are identical" case).
2. Issue many concurrent fetch requests with distinct negotiation payloads (different `want`/`have` sets) so each produces a distinct `computeCacheKey` cache miss.
3. Observe that each miss invokes `s.runPackObjects` (unlimited path) rather than `s.runPackObjectsLimited`, per the branch at: [3](#0-2) 
4. Confirm via Prometheus metrics / process listing that the number of concurrent `git-pack-objects` processes exceeds any configured `[pack_objects_limiting]` concurrency limit, demonstrating the limiter bypass.

### Citations

**File:** internal/gitaly/service/hook/pack_objects.go (L65-86)
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
```

**File:** internal/gitaly/service/hook/pack_objects.go (L147-212)
```go
func (s *server) runPackObjects(
	ctx context.Context,
	w io.Writer,
	req *gitalypb.PackObjectsHookWithSidechannelRequest,
	args *packObjectsArgs,
	stdin io.ReadCloser,
	key string,
) error {
	// We want to keep the context for logging, but we want to block all its
	// cancellation signals (deadline, cancel etc.). This is because of
	// the following scenario. Imagine client1 calls PackObjectsHook and
	// causes runPackObjects to run in a goroutine. Now suppose that client2
	// calls PackObjectsHook with the same arguments and stdin, so it joins
	// client1 in waiting for this goroutine. Now client1 hangs up before the
	// runPackObjects goroutine is done.
	//
	// If the cancellation of client1 propagated into the runPackObjects
	// goroutine this would affect client2. We don't want that. So to prevent
	// that, we suppress the cancellation of the originating context.
	ctx = context.WithoutCancel(ctx)

	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	defer stdin.Close()

	return s.runPackObjectsFn(ctx, s.gitCmdFactory, w, req, args, stdin, key)
}

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
