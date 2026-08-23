Based on my research, the `PackObjectsHookWithSidechannelRequest.RemoteIp` field is directly attacker/GitLab-Rails-controlled data (it's a `gitalypb.PackObjectsHookWithSidechannelRequest` field, populated via `gitcmd.UserDetails.RemoteIP` sourced from gRPC metadata `remote_ip`, not from the actual TCP peer address seen by Gitaly), and it is used as the exclusive gate for whether the per-IP concurrency limiter applies.

### Title
Client-supplied `RemoteIp` field lets requests bypass pack-objects concurrency limiting - ([File: internal/gitaly/service/hook/pack_objects.go])

### Summary
`packObjectsHook` decides whether to apply the `pack-objects` concurrency limiter based solely on whether `req.GetRemoteIp()` parses to a non-loopback IP address. Because this value flows from client/gitlab-shell-supplied metadata rather than the verified network peer address, a caller can force it to `127.0.0.1` (or an unparsable string) to have Gitaly treat any number of expensive `git pack-objects` invocations as "local" and skip the limiter entirely.

### Finding Description
In `packObjectsHook`, the request's remote IP is parsed and, if it is a loopback address (or fails to parse into an IP that is non-loopback), the code takes the unlimited path: [1](#0-0) 

```go
servedBytes, created, err := s.packObjectsCache.Fetch(ctx, cacheKey, output, func(w io.Writer) error {
    ipAddr := net.ParseIP(req.GetRemoteIp())
    ...
    // Ignore loop-back IPs
    if ipAddr != nil && !ipAddr.IsLoopback() {
        return s.runPackObjectsLimited(...)
    }
    return s.runPackObjects(ctx, w, req, args, stdin, cacheKey)
})
```

The `RemoteIp` value originates from the `PackObjectsHookWithSidechannelRequest` proto field, which `gitaly-hooks` populates from the `remote_ip` gRPC metadata value rather than from any cryptographically-verified transport-level peer address: [2](#0-1) 

The per-RPC concurrency/DoS protection (`TypePackObjects` limiter, keyed by remote IP/repo/user) exists specifically to bound resource-intensive `pack-objects` operations: [3](#0-2) 

Since the gate that decides whether this protection applies is based on attacker-influenced metadata, and the code explicitly special-cases "loop-back" as exempt from limiting, a party that can influence the `remote_ip` metadata for their own pushes/fetches (or simply supply/misreport `127.0.0.1` in the hook request) makes their fetch/pack-objects traffic invisible to the limiter, defeating the concurrency-based backpressure mechanism documented in `doc/backpressure.md`.

### Impact Explanation
This maps to "RPC-handler resource limits" bypass: an ordinary user can craft an input field consumed by a resource-limiting decision (the `RemoteIp` field), causing Gitaly to skip queuing/limiting of expensive `git pack-objects` invocations that would otherwise be capped by `defaultPackObjectsLimitingConcurrency`/`defaultPackObjectsLimitingQueueSize`. Repeated concurrent clones/fetches from such a client can consume unbounded CPU/memory on the Gitaly node, causing a denial of service for other tenants of the same node — directly analogous to the external report's pattern of an ordinary caller supplying crafted parameters to an intentionally-permissive/unauthenticated code path in order to defeat a protective check.

### Likelihood Explanation
The `runPackObjects`/`runPackObjectsLimited` split is only reached through `PackObjectsHookWithSidechannelRequest`, which is generated internally by `gitaly-hooks` during upload-pack, not directly exposed to arbitrary gRPC clients; however, the value it forwards as `RemoteIp` is sourced from gRPC metadata (`remote_ip`) that is set by the calling layer per-request and is not independently re-verified against the actual peer address by Gitaly at this decision point. The exact trust boundary for the `remote_ip` metadata value (whether it is authenticated/pinned upstream by Praefect/gitlab-shell or fully attacker-influenced) could not be fully confirmed from the indexed code alone.

### Recommendation
Derive the limiter-exemption decision from the actual verified transport peer address (or from a value that cannot be forged by the requesting party), rather than trusting a request-supplied `RemoteIp` field. At minimum, do not treat unparsable/missing `RemoteIp` values as automatically exempt from limiting — default to applying the limiter when the value cannot be positively verified as loopback from a trusted source.

### Proof of Concept
1. Trigger any `git-upload-pack`/clone flow whose `PackObjectsHookWithSidechannelRequest.RemoteIp` field is set (or left as an unparsable value) such that `net.ParseIP` fails or resolves to a loopback address.
2. Observe in `packObjectsHook` that `runPackObjectsLimited` (which enforces `TypePackObjects` concurrency limits) is skipped in favor of the unbounded `runPackObjects` path.
3. Issue many concurrent such requests to exhaust the Gitaly node's CPU/memory for `git pack-objects`, which the concurrency limiter was designed to prevent, per `doc/backpressure.md`.

Note: I could not fully trace, within the indexed portion of the codebase, the exact upstream code path that sets the `remote_ip` gRPC metadata (e.g., whether Praefect/gitlab-shell sanitizes or authenticates it before it reaches Gitaly), so the full exploitability of forging this value from an untrusted client is not certain. If more certainty is needed, I'd recommend starting a Devin session with full repository access to trace `remote_ip` metadata injection from gitlab-shell/Workhorse/Praefect through to this consumer.

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

**File:** internal/git/gitcmd/hooks_options.go (L57-94)
```go
// WithPackObjectsHookEnv provides metadata for gitaly-hooks so it can act as a pack-objects hook.
func WithPackObjectsHookEnv(objectHash git.ObjectHash, repo *gitalypb.Repository, protocol string) CmdOpt {
	return func(ctx context.Context, cfg config.Cfg, gitCmdFactory CommandFactory, cc *cmdCfg) error {
		if !cfg.PackObjectsCache.Enabled {
			return nil
		}

		if repo == nil {
			return fmt.Errorf("missing repo: %w", ErrInvalidArg)
		}

		userDetails := &UserDetails{
			Protocol: protocol,
			UserID:   metadata.GetValue(ctx, "user_id"),
			Username: metadata.GetValue(ctx, "username"),
			RemoteIP: metadata.GetValue(ctx, "remote_ip"),
		}

		if err := cc.configureHooks(
			ctx,
			cfg,
			objectHash,
			repo,
			gitCmdFactory,
			userDetails,
			PackObjectsHook,
		); err != nil {
			return fmt.Errorf("pack-objects hook configuration: %w", err)
		}

		cc.globals = append(cc.globals, ConfigPair{
			Key:   "uploadpack.packObjectsHook",
			Value: cfg.BinaryPath("gitaly-hooks"),
		})

		return nil
	}
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
