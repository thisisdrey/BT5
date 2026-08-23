### Title
Empty concurrency-limiter lock key silently bypasses per-RPC resource limiting - ([File: internal/grpc/middleware/limithandler/middleware.go])

### Summary
Gitaly's concurrency-limiting middleware derives a "lock key" from the incoming request (typically the target repository's relative path) and only applies the configured per-RPC/per-repository concurrency limiter when that key is non-empty. When the key is empty, the interceptor takes the "no limiting" branch and forwards the request directly to the handler, exactly mirroring the reported Olympus bug class: a companion field that is *expected* to be populated whenever the protective feature is enabled is never validated, and an empty/zero value silently disables the protection instead of being rejected or defaulted safely.

### Finding Description
`LimitConcurrencyByRepo` returns the repository's relative path extracted from `requestinfohandler`, or `""` if no `RequestInfo` was injected or the repository could not be resolved: [1](#0-0) 

Both `UnaryInterceptor` and the stream `RecvMsg` wrapper immediately short-circuit to the un-limited code path whenever this key is empty, bypassing lookup of `methodLimiters`/`methodLimitersUnauthenticated` entirely: [2](#0-1) [3](#0-2) 

The lock key is derived from `RequestInfo.Repository`, which is populated by `extractRequestInfo` via reflection over the request's target-repository field for repository-scoped RPCs — but this extraction can legitimately yield an empty `RelativePath` (e.g., an incomplete/malformed `Repository` message, a repository field that is present but unset, or an RPC whose scope isn't `ScopeRepository` so `Repository` is never even set): [4](#0-3) [5](#0-4) 

This is structurally the same defect as the Olympus report: `useSubmodules == true` implies `submoduleReservesSelector` should be set, but the code never checks it before dereferencing/using it, so the "empty" case is handled by an unintended and unsafe fallback path rather than an explicit validation error. Here, "concurrency limiting configured for this RPC" implies "lock key should be a valid, non-empty repository identifier," but nothing enforces that invariant — an empty key silently disables limiting instead of erroring or falling back to a safe default (e.g., a global/method-wide key).

### Impact Explanation
Per-RPC/per-repository concurrency limiters (`WithConcurrencyLimiters`) exist specifically to bound resource exhaustion from repository-scoped mutator/accessor RPCs. If an ordinary, unprivileged client can cause the lock key to resolve to an empty string for a limited RPC (via a malformed/incomplete repository field or a mis-scoped request), that specific call bypasses concurrency limiting entirely. Under sustained load this defeats the resource-limiting control and enables denial-of-service against the Gitaly node — the same class of impact ("some category can't get enforced behavior" → here "some requests can't be rate/concurrency limited") as the original finding, just manifesting as a control bypass instead of a revert.

### Likelihood Explanation
Likelihood is moderate: the request info is derived automatically per RPC via `protoregistry` method metadata and does not require any privileged access — any client capable of invoking a concurrency-limited RPC can potentially trigger this path. However, exploitability depends on being able to reach a code path where `RequestInfo.Repository` ends up nil/empty (or scope mismatch) while still routing to a limiter-guarded method; this requires further empirical confirmation of which registered RPCs and configurations combine to produce an empty lock key for a configured `concurrency.RPC` entry. I could not fully verify from the index which specific RPCs are affected without running the actual configuration end-to-end.

### Recommendation
When a concurrency limiter is configured for a given method, treat an empty lock key as an error condition to fail closed (e.g., default to a global per-method key, or reject with an internal error) instead of silently skipping the limiter. Concretely, in `UnaryInterceptor`/`RecvMsg`, change:
```go
lockKey := c.getLockKey(ctx)
if lockKey == "" {
    return handler(ctx, req)
}
```
to only skip limiting when there is genuinely no limiter configured for the method, and otherwise fall back to a well-defined key (e.g., `info.FullMethod`) rather than bypassing the limiter outright.

### Proof of Concept
Not independently reproduced from the index; this would require crafting/verifying a specific RPC and configuration (`concurrency.RPC` entry) where `requestinfohandler.RequestInfo.Repository` resolves to `nil`/empty `RelativePath` while a limiter is configured for that method, and confirming request throughput is unbounded in that case. This should be validated with a live Gitaly instance/test harness (e.g., via `internal/grpc/middleware/limithandler/middleware_test.go`) before treating this as fully confirmed.

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

**File:** internal/grpc/middleware/limithandler/middleware.go (L75-98)
```go
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

**File:** internal/grpc/middleware/limithandler/middleware.go (L118-147)
```go
func (w *wrappedStream) RecvMsg(m interface{}) error {
	if err := w.ServerStream.RecvMsg(m); err != nil {
		return err
	}

	// Only perform limiting on the first request of a stream
	if !w.initial {
		return nil
	}

	w.initial = false

	ctx := w.Context()

	lockKey := w.limiterMiddleware.getLockKey(ctx)
	if lockKey == "" {
		return nil
	}

	// Check if request is authenticated
	limiter := w.limiterMiddleware.methodLimiters[w.info.FullMethod]
	unauthLimiter, ok := w.limiterMiddleware.methodLimitersUnauthenticated[w.info.FullMethod]
	if !auth.IsAuthenticated(ctx) && ok {
		limiter = unauthLimiter
	}

	if limiter == nil {
		// No concurrency limiting
		return nil
	}
```

**File:** internal/grpc/middleware/requestinfohandler/requestinfohandler.go (L43-63)
```go
type RequestInfo struct {
	correlationID   string
	FullMethod      string
	methodType      string
	clientName      string
	remoteIP        string
	userID          string
	userName        string
	callSite        string
	callerID        string
	authVersion     string
	deadlineType    string
	methodOperation string
	methodScope     string

	Repository       *gitalypb.Repository
	sourceRepository *gitalypb.Repository
	objectPool       *gitalypb.ObjectPool
	storageName      string
	partition        storage.PartitionID
}
```

**File:** internal/grpc/middleware/requestinfohandler/requestinfohandler.go (L204-223)
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
			case protoregistry.ScopePartition:
				if storage, err := mi.Storage(reqMsg); err == nil {
					i.storageName = storage
				}
				if ptn, err := mi.Partition(reqMsg); err == nil {
					i.partition = ptn
				}
			}
		}
	}
```
