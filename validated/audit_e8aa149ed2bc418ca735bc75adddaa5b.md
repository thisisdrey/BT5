### Title
Concurrency limiter can be bypassed by omitting the repository field, allowing unbounded per-RPC resource exhaustion - (File: internal/grpc/middleware/limithandler/middleware.go)

### Summary
Gitaly's only defense against request floods overwhelming a Gitaly node is the concurrency-limiting middleware described in `doc/backpressure.md`, which throttles the number of concurrent/queued RPCs per repository per method. This limiter is keyed by a "lock key" derived from the target repository of the request. Any request for which that lock key comes back empty is exempted from concurrency limiting entirely (whether or not `[[concurrency]]` is configured for that RPC), letting an unauthenticated or ordinary client flood the handler with unlimited concurrent calls.

### Finding Description
`LimitConcurrencyByRepo` is the default `GetLockKey` implementation used to build the per-RPC limiter chain: [1](#0-0) 

It returns `info.Repository.GetRelativePath()`, or `""` if no `RequestInfo` was injected into the context at all. Both the unary and stream interceptors treat an empty lock key as "skip limiting" rather than "use a shared/default key": [2](#0-1) [3](#0-2) 

`RequestInfo.Repository` is populated from the request's target-repository field via `protoregistry.LookupMethod(...).TargetRepo(reqMsg)`: [4](#0-3) 

Because `*gitalypb.Repository` getters are nil-safe, `GetRelativePath()` returns `""` for any request where the repository is unset, malformed, or fails extraction (e.g., `TargetRepo` returns an error) — the interceptor test suite explicitly documents this "unset repository" case as a supported/expected code path: [5](#0-4) 

The consequence: operators configure `[[concurrency]]` limits (e.g., on `PostUploadPackWithSidechannel`) specifically to stop one repository's traffic surge from exhausting Gitaly node resources, as documented in `doc/backpressure.md`: [6](#0-5) 

But any caller can trivially defeat this per-repo throttle for a repository-scoped RPC by sending requests with the repository field empty/unset/invalid — `GetLockKey` returns `""`, and the interceptor short-circuits straight to the handler with zero concurrency accounting, no queueing, and no `max_queue_size`/`max_queue_wait` protection at all.

### Impact Explanation
This directly maps to the CVE-2022-31006 bug class: an ordinary, non-privileged actor can exhaust a critical shared service (here, the Gitaly RPC handler pool / underlying git subprocess and I/O resources) by sending a flood of requests that are entirely unbounded by the configured resource-limiting mechanism, because the mechanism silently no-ops when it cannot compute a lock key. This is a DoS of an RPC handler resource-limit mechanism reachable directly from a normal client, without requiring privileged access, leaked tokens, or a malicious peer/node.

### Likelihood Explanation
Triggering this requires no special privilege — just crafting or replaying an RPC request against a repository-scoped method while leaving/mangling the `Repository` field so that `TargetRepo` extraction fails or returns an empty relative path. Concurrency limiting is opt-in per RPC via `[[concurrency]]` config, so the impact is highest for exactly the RPCs operators have deliberately protected (e.g., large clone/fetch paths) — the same requests an attacker would want unrestricted access to in order to flood Gitaly.

### Recommendation
When `requestinfohandler.Extract(ctx)` returns `nil` or `info.Repository` is unset/invalid, `LimitConcurrencyByRepo` should not return `""` to bypass limiting; it should fall back to a stable, still-limited key (e.g., a fixed sentinel key, or reject the request outright with an invalid-argument style error before it reaches the handler) so that such malformed/repository-less requests are still subject to the configured concurrency queue and cannot be used to circumvent backpressure protections.

### Proof of Concept
1. Configure a concurrency limit on a repository-scoped RPC, e.g.:
```toml
[[concurrency]]
rpc = "/gitaly.SmartHTTPService/PostUploadPackWithSidechannel"
max_per_repo = 1
max_queue_size = 5
```
2. As an ordinary client, send many concurrent RPC invocations for that method but with the `Repository` field left unset (or a malformed one that fails `TargetRepo` extraction).
3. Observe in `LimitConcurrencyByRepo` that `info.Repository.GetRelativePath()` (or `info == nil`) yields `""`, causing the unary/stream interceptor's `lockKey == ""` branch to call `handler(ctx, req)` directly — completely bypassing `methodLimiters`/`methodLimitersUnauthenticated`, `max_per_repo`, and `max_queue_size`, in contrast to well-formed requests which are correctly throttled.

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

**File:** internal/grpc/middleware/limithandler/middleware.go (L75-80)
```go
func (c *LimiterMiddleware) UnaryInterceptor() grpc.UnaryServerInterceptor {
	return func(ctx context.Context, req interface{}, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (interface{}, error) {
		lockKey := c.getLockKey(ctx)
		if lockKey == "" {
			return handler(ctx, req)
		}
```

**File:** internal/grpc/middleware/limithandler/middleware.go (L130-135)
```go
	ctx := w.Context()

	lockKey := w.limiterMiddleware.getLockKey(ctx)
	if lockKey == "" {
		return nil
	}
```

**File:** internal/grpc/middleware/requestinfohandler/requestinfohandler.go (L204-213)
```go
	if reqMsg, ok := request.(proto.Message); ok {
		// This handles extracting nested and non-nested *gitalypb.Repository fields from the request. In cases of
		// multiple such fields, it will choose the one with the `target_repository` extension.
		if mi, err := protoregistry.GitalyProtoPreregistered.LookupMethod(i.FullMethod); err == nil {
			switch mi.Scope {
			case protoregistry.ScopeRepository:
				if targetRepo, err := mi.TargetRepo(reqMsg); err == nil {
					i.Repository = targetRepo
				}
			case protoregistry.ScopeStorage:
```

**File:** internal/grpc/middleware/requestinfohandler/requestinfohandler_test.go (L464-483)
```go
		{
			desc: "unary repository-scoped call with unset repository",
			call: func(t *testing.T, client mockClient) {
				_, err := client.RepositoryInfo(ctx, &gitalypb.RepositoryInfoRequest{
					Repository: nil,
				})

				require.NoError(t, err)
			},
			expectedInfo: &RequestInfo{
				clientName:      "unknown",
				callSite:        "unknown",
				authVersion:     "unknown",
				deadlineType:    "none",
				methodOperation: "accessor",
				methodScope:     "repository",
				methodType:      "unary",
				FullMethod:      "/gitaly.RepositoryService/RepositoryInfo",
			},
			expectedTags: map[string]any{
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
