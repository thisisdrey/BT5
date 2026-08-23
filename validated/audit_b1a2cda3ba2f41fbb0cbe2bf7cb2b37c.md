### Title
Concurrency limiter for `PackObjectsHookWithSidechannel` is bypassed by client-supplied loopback `RemoteIp` value - (File: `internal/gitaly/service/hook/pack_objects.go`)

### Summary
Gitaly enforces a per-IP concurrency limit on `git-pack-objects` invocations that back every fetch/clone (`PackObjectsHookWithSidechannel`), so that many concurrent requests for the same objects from the same client don't spawn unbounded `git-pack-objects` processes. This limiter is only applied when the request's `RemoteIp` field does not parse to a loopback address; loopback values take an entirely unlimited code path. As in the reported EVM finding — where a nested dispatch used the parent's unrestricted gas meter instead of an enforced limit — Gitaly's own resource-limiting logic contains a conditional bypass keyed off an attacker/request-influenced value (`RemoteIp`), which routes execution around the concurrency limiter entirely.

### Finding Description
`packObjectsHook()` decides whether to call the limited or unlimited path purely based on parsing `req.GetRemoteIp()`: [1](#0-0) 

If the IP is `nil` (unparseable) or loopback, it calls the completely unbounded `runPackObjects()`. Only non-loopback, parseable IPs are routed to `runPackObjectsLimited()`, which acquires a token from `s.packObjectsLimiter`: [2](#0-1) 

This mirrors the `dispatchMessage()` bug class: a guard intended to always enforce a resource budget has a bypass branch selected by a value that is not a securely-scoped, server-derived constant, but effectively an untrusted/derived request attribute. The project's own test suite documents this exact behavior — a request with `RemoteIp: "127.0.0.1"` is explicitly expected `shouldLimit: false`: [3](#0-2) 

`RemoteIp` on `PackObjectsHookWithSidechannelRequest` is populated end-to-end from the `HooksPayload.UserDetails.RemoteIP` value that is embedded into the hook's environment and forwarded by `gitaly-hooks` back into gRPC metadata for the call to `HookService`: [4](#0-3) [5](#0-4) 

### Impact Explanation
The `[[concurrency]]`/`packObjectsLimiter` mechanism exists specifically as backpressure against I/O/CPU exhaustion from concurrent `git-pack-objects` invocations, as documented in `doc/backpressure.md`: [6](#0-5) 

If the `RemoteIP` value that ultimately reaches this check can be influenced (e.g., because the value derives from client-controllable/forwarded connection metadata for a given protocol/deployment, or because multiple distinct end users are ever reported as loopback under NAT/proxy setups), then a client can force fan-out `git-pack-objects` executions for the same repository/objects to skip the limiter entirely and consume unbounded server resources — a resource-exhaustion / DoS vector on the RPC handler, directly analogous to bypassing a resource cap that was meant to be unconditionally enforced.

### Likelihood Explanation
This requires confirming, outside of Gitaly's own code (in GitLab Workhorse/Rails or whatever component populates `UserDetails.RemoteIP` before it reaches Gitaly), whether an ordinary user can cause their own `RemoteIP` to be recorded/forwarded as loopback (e.g. via proxy headers, or by connecting through a path where the "real" IP is legitimately reported as `127.0.0.1`). This external dependency could not be verified within the Gitaly repository, so likelihood is uncertain and depends on that upstream trust boundary; the vulnerability inside Gitaly itself — the loopback branch that fully disables limiting — is concretely present and intentional/undocumented as a security boundary risk.

### Recommendation
- Do not treat "loopback" as an unconditionally trusted bypass signal for the `packObjectsLimiter`, since `RemoteIp` originates from data that traverses hook/env/gRPC-metadata plumbing rather than a hard network-layer guarantee.
- If loopback exemption is intended only for genuinely internal Gitaly-originated calls, gate it on a value derived independently of the hooks payload (e.g., an internal auth token or explicit "internal call" context marker) rather than the `RemoteIp` string.
- At minimum, apply a lower-but-nonzero shared/global limiter for the loopback path, mirroring the concept in the referenced report's fix of always creating a bounded, scoped execution budget instead of a full bypass.

### Proof of Concept
Not independently reproduced end-to-end (would require confirming how `UserDetails.RemoteIP` is populated in the SSH/SmartHTTP request path outside this repo), but the code-level condition is directly demonstrated by Gitaly's own test: [3](#0-2) 

Two `PackObjectsHookWithSidechannelRequest`s with identical `GlId`, `Repository`, and `Args` but `RemoteIp: "127.0.0.1"` are asserted to **not** be concurrency-limited (`shouldLimit: false`), whereas identical requests with a normal IP **are** limited (`shouldLimit: true`, lines 771-795), confirming the bypass branch in `packObjectsHook()` (`pack_objects.go:74`).

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

**File:** internal/gitaly/service/hook/pack_objects_test.go (L821-845)
```go
		{
			desc: "IPv4 loopback addresses",
			setup: func(t *testing.T, cfg config.Cfg) [2]*gitalypb.PackObjectsHookWithSidechannelRequest {
				repo, _ := gittest.CreateRepository(t, ctx, cfg)
				hooksPayloadEnv := hooksPayloadEnvForRepository(t, ctx, cfg, repo)

				return [2]*gitalypb.PackObjectsHookWithSidechannelRequest{
					{
						GlId:                 "user-123",
						RemoteIp:             "127.0.0.1",
						Repository:           repo,
						Args:                 args,
						EnvironmentVariables: hooksPayloadEnv,
					},
					{
						GlId:                 "user-123",
						RemoteIp:             "127.0.0.1",
						Repository:           repo,
						Args:                 args,
						EnvironmentVariables: hooksPayloadEnv,
					},
				}
			},
			shouldLimit: false,
		},
```

**File:** cmd/gitaly-hooks/hooks.go (L168-185)
```go
func injectMetadataIntoOutgoingCtx(ctx context.Context, payload gitcmd.HooksPayload) context.Context {
	if payload.UserDetails != nil {
		ctx = metadata.AppendToOutgoingContext(
			ctx,
			"user_id",
			payload.UserDetails.UserID,
			"username",
			payload.UserDetails.Username,
			"remote_ip",
			payload.UserDetails.RemoteIP,
		)
	}

	for _, flag := range payload.FeatureFlagsWithValue {
		ctx = featureflag.OutgoingCtxWithFeatureFlag(ctx, flag.Flag, flag.Enabled)
	}
	return ctx
}
```

**File:** internal/git/gitcmd/hooks_payload.go (L109-123)
```go
// UserDetails contains all information which is required for hooks
// executed by git-receive-pack, namely the pre-receive, update or post-receive
// hook.
type UserDetails struct {
	// Username contains the name of the user who has caused the hook to be executed.
	Username string `json:"username"`
	// UserID contains the ID of the user who has caused the hook to be executed.
	UserID string `json:"userid"`
	// Protocol contains the protocol via which the hook was executed. This
	// can be one of "web", "ssh" or "smarthttp".
	Protocol string `json:"protocol"`
	// RemoteIP contains the original IP of the client who initiated the flow leading to this
	// target hook.
	RemoteIP string `json:"remote_ip"`
}
```

**File:** doc/backpressure.md (L1-24)
```markdown
# Request limiting in Gitaly

In the GitLab ecosystem, Gitaly is the service that is at the bottom of the
stack for Git data access. This means that when there is a surge of
requests to retrieve or change a piece of Git data, the I/O happens in Gitaly.
This can lead to Gitaly being overwhelmed due to system resource exhaustion
because all Git access goes through Gitaly.

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
