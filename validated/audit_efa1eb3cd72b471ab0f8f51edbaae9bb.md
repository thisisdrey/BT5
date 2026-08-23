Confirmed: `remote_ip` is read directly from incoming gRPC metadata sent by the calling client [1](#0-0) , and this same client-supplied value is what ends up in `WithPackObjectsHookEnv`'s `UserDetails.RemoteIP` [2](#0-1) , which is then forwarded verbatim into the `PackObjectsHookWithSidechannelRequest.remote_ip` field used purely as the concurrency-limiting key [3](#0-2) .

### Title
Unverified `remote_ip` field allows bypass of PackObjectsHook concurrency limiting via spoofed loopback address - (File: internal/gitaly/service/hook/pack_objects.go)

### Summary
`PackObjectsHookWithSidechannel` uses the client-supplied `remote_ip` field solely to decide whether to apply the per-IP concurrency limiter, without verifying that this value corresponds to the actual origin of the request. Similar to the H-1 report's `_srcChainSender` issue — where a message parameter identifying the "sender" was trusted without verification, letting an attacker impersonate another sender and bypass sender-scoped checks — here the `remote_ip` "identity" of the fetch initiator is trusted without verification, letting a caller impersonate a loopback client and bypass the resource-limiting check entirely.

### Finding Description
The `remote_ip` field originates from an incoming gRPC metadata key (`"remote_ip"`) that is read as-is from the request without any cross-check against the actual peer/connection address [1](#0-0) . It flows through `metadata.GetValue(ctx, "remote_ip")` into `UserDetails.RemoteIP` when constructing the pack-objects hook environment [2](#0-1) , then into the `gitaly-hooks` `packObjectsHook` command which reads it straight from the hook payload and puts it into the `PackObjectsHookWithSidechannelRequest.RemoteIp` field sent back to Gitaly [4](#0-3) .

In `packObjectsHook`, this field is the sole basis for deciding whether the concurrency limiter (`runPackObjectsLimited`) applies at all: [3](#0-2) 

If the parsed IP is a loopback address (or fails to parse), the code falls through to the unlimited `runPackObjects` path, entirely skipping the per-remote-IP concurrency limiting that is meant to prevent one client/IP from exhausting `git-pack-objects` resources.

### Impact Explanation
Because the field is taken from an arbitrary, attacker/client-influenceable metadata value rather than the actual TCP peer address, a caller reaching this RPC (or values propagated into the hook payload for it) can set/forge `remote_ip` as `"127.0.0.1"` (or any unparsable string) to make the server treat every request as coming from the loopback and thereby completely evade the resource/DoS protection intended to cap concurrent `git-pack-objects` invocations per remote IP. This weakens a DoS-mitigation control on a handler that is reachable as part of ordinary fetch/clone flows, allowing resource exhaustion on the Gitaly node (RPC-handler resource-limit bypass).

### Likelihood Explanation
`PackObjectsHookWithSidechannel` is invoked for every fetch/clone that uses the pack-objects cache/hook feature, i.e., from an ordinary user's fetch operation. The `remote_ip` value traces back to gRPC metadata that is set/forwarded on a per-connection basis by Workhorse/gitlab-shell and relayed through Gitaly's hook payload rather than being independently derived from the transport-layer peer address at the point of use, so the trust boundary here is weaker than expected. Exploitability depends on whether an actor controlling a client component (e.g., a compromised or misbehaving intermediate proxy/shell integration) can set this metadata value freely — a scenario consistent with "crafted RPC field" reachable through ordinary fetch traffic, though it does require some level of control over the metadata sent to Gitaly rather than a fully anonymous end user.

### Recommendation
Do not rely on a client-supplied `remote_ip` string for a security-relevant decision (limiter bypass). Instead, derive the real remote IP from the authenticated connection/peer info at the point closest to the actual network socket, and if it must be threaded through the hook payload, cryptographically bind it (e.g., as part of the already-signed internal socket token payload) so that intermediate hook processes cannot substitute an arbitrary value. At minimum, treat unparsable or loopback-claimed IPs conservatively (fail closed to the limited path) rather than defaulting to unlimited execution.

### Proof of Concept
1. Trigger a fetch/clone that goes through `gitaly-hooks`' `packObjectsHook`, ensuring the hook payload's `UserDetails.RemoteIP` is influenced with the value `"127.0.0.1"` (or any non-parseable string).
2. Observe that `PackObjectsHookWithSidechannelRequest.RemoteIp` carries this same forged value into `packObjectsHook` in `internal/gitaly/service/hook/pack_objects.go`.
3. In `packObjectsHook`, `net.ParseIP(req.GetRemoteIp())` returns a loopback address (or `nil` cannot be split into host/port), so the `!ipAddr.IsLoopback()` check is false and the request goes to unlimited `s.runPackObjects` instead of `s.runPackObjectsLimited`.
4. Repeat concurrently from what should be counted as the same limited IP; because each request can carry a distinct forged `remote_ip` (or all claim loopback), the per-IP concurrency cap is never enforced, allowing unrestricted concurrent `git-pack-objects` invocations and consequent resource exhaustion.

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
