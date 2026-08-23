### Title
Concurrency Limiter Uses Unvalidated Client-Supplied `RelativePath` as Lock Key, Allowing Per-Repository Resource-Limit Duplication - ([File: internal/grpc/middleware/limithandler/middleware.go])

### Summary
Gitaly's per-repository concurrency limiter keys its accounting entirely off the raw `RelativePath` string taken from the client's gRPC request, before that path is validated or canonicalized. An ordinary user who can issue requests against their own repository (e.g. via `PostUploadPackWithSidechannel` for clone/fetch) can supply syntactically different but semantically-equivalent relative path strings that the locator will later resolve to the very same on-disk repository. Because the limiter's bookkeeping map is keyed by the raw string rather than the canonical path, each distinct string variant gets its own independent `max_per_repo`/`max_queue_size` slot, letting a single client multiply the number of concurrently-admitted expensive Git subprocess invocations against one physical repository well beyond the configured cap.

### Finding Description
`LimitConcurrencyByRepo` in `internal/grpc/middleware/limithandler/middleware.go` defines the lock key used by the concurrency limiter as simply: [1](#0-0) 

`info.Repository` here comes from `requestinfohandler`'s `extractRequestInfo`, which pulls the target repository message straight out of the incoming proto request via `mi.TargetRepo(reqMsg)`, with no cleaning, normalization, or validation of the `RelativePath` field: [2](#0-1) 

This raw string is used as the map key inside `ConcurrencyLimiter`, which lazily creates a `keyedConcurrencyLimiter` per distinct key: [3](#0-2) [4](#0-3) 

The documented purpose of this mechanism is explicit per-repository backpressure — e.g. capping concurrent clone/fetch RPCs per repository to prevent resource exhaustion: [5](#0-4) 

The limiter runs in the gRPC interceptor chain *before* the actual RPC handler executes, and it is only downstream in the handler (via `locator.GetRepoPath`/`ValidateRepository`) that a `RelativePath` is canonicalized/validated against the filesystem. This is the same class of bug described in the reference report: an accounting mechanism (there, deposited-balance checkpoints; here, per-repository concurrency slots) is keyed on an identity value (there, account address; here, raw relative-path string) that can be manipulated by the caller to refer to the same underlying resource under multiple distinct identities, without the accounting system ever reconciling that they are the same resource. Just as Alice/Bob could inflate their combined "balance" beyond what was actually deposited by using multiple addresses, a client can inflate the number of concurrently-admitted expensive RPCs against a single physical repository beyond the intended `max_per_repo` cap by using multiple path spellings.

### Impact Explanation
The `[[concurrency]]` limiter is Gitaly's primary defense against a single repository (or client) monopolizing I/O/CPU resources by issuing many concurrent expensive Git operations. If the lock key can be duplicated by using different but resolvable-to-identical `RelativePath` strings, an authenticated (and even unauthenticated, per `Unauthenticated` limiter config) client can bypass `max_per_repo` and `max_queue_size` and drive up concurrent execution of costly handlers such as `PostUploadPackWithSidechannel`, defeating the very DoS-prevention mechanism documented in `doc/backpressure.md` and `doc/load-management-architecture.md`. This is a resource-limit-bypass / DoS-of-a-handler class issue, one of the accepted impact categories.

### Likelihood Explanation
Likelihood assessment is **uncertain and not fully proven** given available tooling. I was not able to confirm within this session whether:
1. The `locator`'s later canonicalization (`GetRepoPath`/`ValidateRepository`, in `internal/gitaly/config/locator.go`) actually accepts multiple distinct raw string spellings (e.g., trailing slash, redundant `./`, `.git` suffix variants, case differences) as valid and resolves them to the same on-disk repository, or whether Gitaly performs strict path validation/rejection of any non-canonical form earlier in request handling (possibly even before the limiter middleware runs, depending on interceptor chain ordering) that would foreclose this exact bypass.
2. The relative interceptor ordering in `internal/gitaly/server/server.go` places `requestinfohandler`/`limithandler` before any path-canonicalization step, or whether some canonicalization happens earlier than the limiter runs.

Without confirming these two points, this remains a plausible but not fully demonstrated bypass — the code structurally supports the weakness (raw, unvalidated string as accounting key), but I could not verify a concrete client-observable multiple-strings-same-repo case within the available search budget.

### Recommendation
Canonicalize the repository identity used as the concurrency-limiter lock key (e.g., resolve via the storage locator to an absolute/canonical on-disk path, or at minimum apply `filepath.Clean` plus consistent trailing-slash/suffix normalization) before using it in `LimitConcurrencyByRepo`, so that logically identical repositories always collapse to a single limiter key regardless of how the client spells the `RelativePath`.

### Proof of Concept
Not fully constructed/verified — this would require confirming, via a running Gitaly instance, that two `RepositoryInfo`-scoped requests (e.g. two `PostUploadPackWithSidechannel` calls) using textually different `RelativePath` values that the locator resolves to the same physical repository path are admitted as two independent concurrency slots by the `[[concurrency]] max_per_repo = 1` limiter, rather than being serialized as expected. I could not execute this verification within the current session and flag it as an open item for further investigation (e.g., via a Devin session with repo/filesystem access) rather than asserting it as confirmed.

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

**File:** internal/grpc/middleware/requestinfohandler/requestinfohandler.go (L204-222)
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
```

**File:** internal/limiter/concurrency_limiter.go (L131-156)
```go
// ConcurrencyLimiter contains rate limiter state.
type ConcurrencyLimiter struct {
	// limit is the adaptive maximum number of concurrent calls to the limited function. This limit is
	// calculated adaptively from an outside calculator.
	limit *AdaptiveLimit
	// maxQueueLength is the maximum number of operations allowed to wait in a queued state.
	// This limit is global and applies before the concurrency limit. Subsequent incoming
	// operations will be rejected with an error immediately.
	maxQueueLength int
	// maxQueueWait is a time duration of an operation allowed to wait in the queue.
	maxQueueWait time.Duration
	// SetWaitTimeoutContext is a function for setting up timeout context. If this is nill, context.WithTimeout is
	// used. This function is for internal testing purpose only.
	SetWaitTimeoutContext func() context.Context

	// monitor is a monitor that will get notified of the state of concurrency-limited RPC
	// calls.
	monitor ConcurrencyMonitor

	m sync.RWMutex
	// limitsByKey tracks all concurrency limits per key. Its per-key entries are lazily created
	// and will get evicted once there are no concurrency-limited calls for any such key
	// anymore.
	limitsByKey map[string]*keyedConcurrencyLimiter
}

```

**File:** internal/limiter/concurrency_limiter.go (L197-212)
```go
func (c *ConcurrencyLimiter) Limit(ctx context.Context, limitingKey string, f LimitedFunc) (interface{}, error) {
	span, ctx := tracing.StartSpanIfHasParent(
		ctx,
		"limiter.ConcurrencyLimiter.Limit",
		[]attribute.KeyValue{
			attribute.String("key", limitingKey),
		},
	)
	defer span.End()

	if c.currentLimit() <= 0 {
		return f()
	}

	sem := c.getConcurrencyLimit(limitingKey)
	defer c.putConcurrencyLimit(limitingKey)
```

**File:** doc/backpressure.md (L13-42)
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
