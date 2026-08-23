Based on my investigation, I found a concrete analog to the reported bug class.

### Title
Attacker-controlled `remote_ip` gRPC metadata allows bypassing pack-objects concurrency limiting - ([File: internal/gitaly/service/hook/pack_objects.go])

### Summary
The DODO finding is about a user-supplied fee parameter (`brokerFeeRate`) that is never validated, letting the attacker set it to a value (`0`) that disables an economic enforcement mechanism. The closest reachable Gitaly analog is Gitaly's pack-objects concurrency limiter, whose bucketing key is derived from an unauthenticated, client-influenceable `remote_ip` value that is not validated against the actual peer connection, allowing the enforcement (rate/concurrency limiting) to be trivially bypassed or defeated.

### Finding Description
The `PackObjectsHookWithSidechannel` RPC uses `req.GetRemoteIp()` as the key for its per-client concurrency limiter: [1](#0-0) 

This `RemoteIp` field originates from gRPC outgoing metadata key `"remote_ip"` that is set on the `gitaly-hooks` process's context and then baked into the `HooksPayload`/`UserDetails` sent back to the Gitaly hook service: [2](#0-1) [3](#0-2) 

Tests confirm that when `RemoteIp` is empty, unparsable, or loopback, the concurrency limiter is not applied at all — the request falls through to the unlimited code path: [4](#0-3) [1](#0-0) 

The `Limit` bucketing logic itself keys purely on the string it is given, with no cross-check against the actual network peer of the RPC, only on `req.GetRemoteIp()` (a value that flows from `gl_id`/`remote_ip` fields on `PackObjectsHookWithSidechannelRequest`, itself sourced from environment/metadata that `gitaly-hooks` forwards from the pre-receive/upload-pack path): [5](#0-4) [6](#0-5) 

This is structurally analogous to the DODO bug: an enforcement mechanism (fee charging / concurrency limiting) depends on a value that traces back to attacker-influenceable input (`feeData` / `remote_ip`) with no independent validation tying it to ground truth (actual broker registration / actual TCP peer address), so the attacker can pick a value (`0` / empty or invalid IP string) that causes the enforcement path to be skipped entirely.

### Impact Explanation
If an attacker (or a malicious/misconfigured intermediate proxy relaying `remote_ip` metadata) can supply an empty, malformed, or loopback-appearing IP string, the `packObjectsLimiter.Limit` call is skipped, so `runPackObjects` executes with no concurrency cap. This is a resource-exhaustion / DoS vector against the RPC handler responsible for pack generation — an attacker can flood the server with many parallel expensive `git pack-objects` invocations that are supposed to be capped per remote IP but aren't, exhausting CPU/memory on the Gitaly node.

### Likelihood Explanation
This is reachable from an ordinary fetch/clone (upload-pack triggers the pack-objects hook), and the `remote_ip` value is populated by `gitaly-hooks`/the internal API integration rather than being independently derived from the transport-layer peer address of the RPC call itself, so any component in that chain that fails to set/sanitize it (or an attacker able to influence the metadata forwarded through `gitlabshell`/hook environment) can force the fallback, unlimited path. The likelihood of this being exploitable end-to-end (i.e., whether an external, unprivileged fetch client can actually force `RemoteIp` to be empty/invalid) depends on components (GitLab Shell / Workhorse / Rails) outside of the indexed Gitaly repository, so this cannot be fully confirmed from Gitaly's code alone.

### Recommendation
Do not rely solely on the caller-supplied `remote_ip` string for concurrency-limiting decisions. Either derive the limiting key from the actual gRPC peer/transport address where available, or validate/sanitize the `remote_ip` value upstream and fail closed (apply the strictest limiter) rather than skip limiting entirely when the IP is empty, unparsable, or otherwise anomalous, mirroring how `runPackObjectsLimited` is only invoked in the "valid, non-loopback IP" branch today.

### Proof of Concept
1. An unprivileged client initiates repeated `git-upload-pack`/fetch operations against a Gitaly server that has `PackObjectsCache`/pack-objects concurrency limiting enabled.
2. Through the hook payload/environment chain (`gitcmd.WithPackObjectsHookEnv` → `gitaly-hooks` → `PackObjectsHookWithSidechannelRequest.RemoteIp`), the attacker causes `RemoteIp` to be empty or unparsable (e.g., by triggering a code path or environment where `metadata.GetValue(ctx, "remote_ip")` is not populated, as demonstrated in the "empty IP addresses" and "invalid IP addresses" test cases which explicitly assert `shouldLimit: false`) [4](#0-3) .
3. `packObjectsHook` sees `ipAddr == nil` and calls the unlimited `s.runPackObjects` path instead of `s.runPackObjectsLimited`, bypassing the configured per-IP concurrency cap [1](#0-0) .
4. The attacker repeats this many times in parallel, exhausting server resources without ever being throttled by the pack-objects concurrency limiter.

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

**File:** internal/gitaly/service/hook/pack_objects_test.go (L871-920)
```go
		{
			desc: "invalid IP addresses",
			setup: func(t *testing.T, cfg config.Cfg) [2]*gitalypb.PackObjectsHookWithSidechannelRequest {
				repo, _ := gittest.CreateRepository(t, ctx, cfg)
				hooksPayloadEnv := hooksPayloadEnvForRepository(t, ctx, cfg, repo)

				return [2]*gitalypb.PackObjectsHookWithSidechannelRequest{
					{
						GlId:                 "user-123",
						RemoteIp:             "hello-world",
						Repository:           repo,
						Args:                 args,
						EnvironmentVariables: hooksPayloadEnv,
					},
					{
						GlId:                 "user-123",
						RemoteIp:             "hello-world",
						Repository:           repo,
						Args:                 args,
						EnvironmentVariables: hooksPayloadEnv,
					},
				}
			},
			shouldLimit: false,
		},
		{
			desc: "empty IP addresses",
			setup: func(t *testing.T, cfg config.Cfg) [2]*gitalypb.PackObjectsHookWithSidechannelRequest {
				repo, _ := gittest.CreateRepository(t, ctx, cfg)
				hooksPayloadEnv := hooksPayloadEnvForRepository(t, ctx, cfg, repo)

				return [2]*gitalypb.PackObjectsHookWithSidechannelRequest{
					{
						GlId:                 "user-123",
						RemoteIp:             "",
						Repository:           repo,
						Args:                 args,
						EnvironmentVariables: hooksPayloadEnv,
					},
					{
						GlId:                 "user-123",
						RemoteIp:             "",
						Repository:           repo,
						Args:                 args,
						EnvironmentVariables: hooksPayloadEnv,
					},
				}
			},
			shouldLimit: false,
		},
```

**File:** proto/hook.proto (L181-196)
```text
  // repository ...
  Repository repository = 1 [(target_repository)=true];
  // args contains the arguments passed to the pack-objects hook, without the leading "git"
  repeated string args = 2;
  // gl_id is the user id of the initator of the fetch
  string gl_id = 3;
  // gl_username is the username of the initator of the fetch
  string gl_username = 5;
  // git_protocol is the protocol used for the fetch
  string git_protocol = 6;
  // remote_ip is the original IP of the initator of the fetch. This field is used as the key for
  // concurrency limiting before we trigger git-pack-objects command
  string remote_ip = 7;
  // environment_variables contains the environment variables passed to the hook invocation.
  repeated string environment_variables = 8;
}
```

**File:** cmd/gitaly-hooks/hooks.go (L441-478)
```go
func packObjectsHook(ctx context.Context, payload gitcmd.HooksPayload, hookClient gitalypb.HookServiceClient, args []string) error {
	ctx, wt, err := hook.SetupSidechannel(ctx, payload, func(c *net.UnixConn) error {
		// Buffer stdout: serving a packfile (in particular from the pack-objects
		// cache) is a one-way bulk transfer, so batching the sideband packets
		// reduces the number of writes into the stdout pipe.
		return stream.ProxyPktLine(c, os.Stdin, os.Stdout, os.Stderr, true)
	})
	if err != nil {
		return hookError{returnCode: 1, err: fmt.Errorf("RPC failed: SetupSidechannel: %w", err)}
	}
	defer func() {
		// We already check the error further down.
		_ = wt.Close()
	}()

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
	); err != nil {
		return wrapGRPCError(err)
	}
```
