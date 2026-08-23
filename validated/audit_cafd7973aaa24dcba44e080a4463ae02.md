Confirmed: `remote_ip` is read directly from incoming gRPC metadata (`getFromMD(md, "remote_ip")`) with no validation that it matches the actual peer connection [1](#0-0) , and this same untrusted value flows into `RemoteIP` used by the pack-objects hook, both via `WithPackObjectsHookEnv`/`WithReceivePackHooks` (`metadata.GetValue(ctx, "remote_ip")`) [2](#0-1)  and via `gitaly-hooks` forwarding `payload.UserDetails.RemoteIP` verbatim into `PackObjectsHookWithSidechannelRequest.RemoteIp` [3](#0-2) .

### Title
DoS via spoofable `remote_ip` bypassing pack-objects concurrency limiter - (File: internal/gitaly/service/hook/pack_objects.go)

### Summary
`PackObjectsHookWithSidechannel` decides whether to enforce the `packObjectsLimiter` concurrency/queue limit based solely on the `RemoteIp` field of the request: if the parsed IP is a loopback address, the limiter is skipped entirely and `git-pack-objects` runs unthrottled [4](#0-3) . This mirrors the Aleo bug class: a specific, narrow code path (loopback IP) is exempted from the general resource-gating mechanism (concurrency limiter ≈ fee commitment), and the value that selects this exempted path is attacker-influenceable rather than independently verified.

### Finding Description
The `remote_ip` value is taken from gRPC metadata without any authentication or cross-check against the actual TCP peer address: `newRequestInfo` reads it with `getFromMD(md, "remote_ip")` [1](#0-0) . This same metadata value is what populates `UserDetails.RemoteIP` when Gitaly spawns `upload-pack`/`receive-pack` with the pack-objects hook environment [2](#0-1) , which `gitaly-hooks` later forwards as the `RemoteIp` field of `PackObjectsHookWithSidechannelRequest` [3](#0-2) .

In `packObjectsHook`, the handler parses this attacker-influenced `RemoteIp` and only routes through `runPackObjectsLimited` (which enforces `s.packObjectsLimiter`, the per-IP concurrency/queue backpressure mechanism configured via `[pack_objects_cache]`/`PackObjectsLimiting`) when the IP is *not* loopback; loopback values fall through to the unthrottled `runPackObjects` [5](#0-4) . Because the metadata key is set by whatever calls into the gRPC stack ahead of the hook boundary and is not verified against the real socket peer, if an actor can influence this metadata to read as `127.0.0.1` (or any parseable loopback string), the expensive `git pack-objects` invocation escapes the concurrency limiter that is Gitaly's primary DoS backpressure mechanism, as documented in `doc/backpressure.md` [6](#0-5) .

### Impact Explanation
`git-pack-objects` is one of the most CPU/memory-intensive operations Gitaly performs per fetch/clone. The concurrency limiter (`packObjectsLimiter`, configured in `internal/cli/gitaly/serve.go`) exists specifically to cap concurrent pack-objects invocations per key and queue/reject excess requests [7](#0-6) . A bypass of this gate allows an unlimited number of concurrent, unthrottled `git-pack-objects` processes to be spawned for the same effective key, exhausting Gitaly node CPU/memory and degrading or halting service for all repositories on that node — directly analogous to the Aleo report's "free to abuse, no collateral, CPU exhaustion" DoS pattern.

### Likelihood Explanation
Exploitability hinges on whether an ordinary, unprivileged client can cause the `remote_ip` gRPC metadata (read by `requestinfohandler` and separately propagated into the hook's `UserDetails`) to be set to a loopback-parseable value rather than reflecting genuine client-remote-address data. This RPC (`PackObjectsHookWithSidechannel`) is only invoked internally between `gitaly-hooks` and the Gitaly server as part of legitimate `upload-pack`/`receive-pack` processing, and is protected by Gitaly's internal auth token in production deployments. I could not fully verify, from the available index, whether the `remote_ip` metadata value that reaches `requestinfohandler`/`hooks_options.go` is derived strictly from the trusted peer connection at every ingress point (Workhorse/gitlab-shell/Praefect) or whether any of those layers pass through a client-influenceable header into this metadata key. This is the key open question that determines whether this is a genuinely externally-reachable, unprivileged bypass or an internal/trusted-component-only concern (which per the analog rules should be rejected as privileged-actor/no-impact). Given the index does not contain the code that originates/sets the `remote_ip` gRPC metadata at the outermost client-facing entry point (e.g., in Workhorse or gitlab-shell, which are separate repositories not indexed here), this cannot be conclusively confirmed as exploitable by an ordinary end user from this repo alone.

### Recommendation
Do not derive the loopback/limiter-bypass decision from a value carried in gRPC metadata (`remote_ip`) that is set outside of Gitaly's direct control. Instead, determine "loopback" status from the actual authenticated peer address at the socket/sidechannel level (`gitalyhook.GetSidechannel` connection or the gRPC `peer.Peer` of the request that established the sidechannel), or otherwise ensure that the concurrency limiter is applied unconditionally to `PackObjectsHookWithSidechannel` regardless of the claimed `RemoteIp`, using a limiter key that cannot be forged to select an exempted code path (e.g., repository + git args hash, with IP as best-effort refinement only, not as a bypass condition).

### Proof of Concept
Not constructible from the indexed code alone: exploitation requires confirming, in the client-facing ingress layer (Workhorse/gitlab-shell — not present in this repo's index), that the `remote_ip` gRPC metadata forwarded to Gitaly can be influenced by an unprivileged requester to a loopback value. Within this repo, the vulnerable branch itself is directly demonstrable: any caller of `PackObjectsHookWithSidechannel` supplying (directly or via metadata propagation) a `RemoteIp` that parses to a loopback address will have its `git-pack-objects` call routed through `runPackObjects` instead of `runPackObjectsLimited`, i.e., without going through `s.packObjectsLimiter` [5](#0-4) .

### Citations

**File:** internal/grpc/middleware/requestinfohandler/requestinfohandler.go (L160-162)
```go
	if remoteIP := getFromMD(md, "remote_ip"); remoteIP != "" {
		info.remoteIP = remoteIP
	}
```

**File:** internal/git/gitcmd/hooks_options.go (L68-73)
```go
		userDetails := &UserDetails{
			Protocol: protocol,
			UserID:   metadata.GetValue(ctx, "user_id"),
			Username: metadata.GetValue(ctx, "username"),
			RemoteIP: metadata.GetValue(ctx, "remote_ip"),
		}
```

**File:** cmd/gitaly-hooks/hooks.go (L456-475)
```go
	var glID, glUsername, gitProtocol, remoteIP string

	if payload.UserDetails != nil {
		glID = payload.UserDetails.UserID
		glUsername = payload.UserDetails.Username
		gitProtocol = payload.UserDetails.Protocol
		remoteIP = payload.UserDetails.RemoteIP
	}

	if _, err := hookClient.PackObjectsHookWithSidechannel(
		ctx,
		&gitalypb.PackObjectsHookWithSidechannelRequest{
			Repository:           payload.Repo,
			EnvironmentVariables: os.Environ(),
			Args:                 args,
			GlId:                 glID,
			GlUsername:           glUsername,
			GitProtocol:          gitProtocol,
			RemoteIp:             remoteIP,
		},
```

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

**File:** doc/backpressure.md (L13-24)
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
```

**File:** internal/cli/gitaly/serve.go (L392-400)
```go
	packObjectsLimiterAuthenticated := limiter.NewConcurrencyLimiter(
		packObjectLimit,
		cfg.PackObjectsLimiting.QueueMax(),
		cfg.PackObjectsLimiting.MaxQueueWait.Duration(),
		packObjectsMonitor,
	)
	prometheus.MustRegister(packObjectsMonitor)

	var packObjectsLimiter limiter.Limiter = packObjectsLimiterAuthenticated
```
