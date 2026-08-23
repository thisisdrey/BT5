Confirmed: the concurrency limiter's lock key is derived directly from `Repository.GetRelativePath()` as taken raw from the client request (`i.Repository = targetRepo` in `extractRequestInfo`), before any of Gitaly's path-normalization/validation (e.g. `storage.ValidateRelativePath`, which happens later inside the storage/transaction layer). This produces the same "same underlying resource accounted under two different keys" bug class as the report (staking token usable in two accounting systems simultaneously): a client can send RPCs against the same physical repository using two syntactically different but path-equivalent `relative_path` strings (e.g. with/without a trailing slash, redundant `./`, or a `../x/` traversal that collapses to the same target) so that the raw strings differ and hash to different `limitsByKey` map entries in `ConcurrencyLimiter`, while `storage.ValidateRelativePath` later normalizes both to the identical on-disk path. This lets the attacker exceed `max_per_repo`/`max_concurrency` and `max_queue_size` for a single repository by "aliasing" it under multiple raw path strings, defeating the RPC-handler resource limit (DoS on a hot repository) even though the backend still serializes/does I/O against one physical repository.

### Title
Per-Repository Concurrency Limiter Uses Unnormalized `relative_path` as Lock Key, Allowing Limit Bypass via Path Aliasing - (File: internal/grpc/middleware/limithandler/middleware.go)

### Summary
`LimitConcurrencyByRepo` keys the per-repository concurrency limiter directly on the raw, client-supplied `Repository.GetRelativePath()` string captured by `requestinfohandler` before Gitaly normalizes the path. Because normalization/canonicalization (`storage.ValidateRelativePath`) happens later in the storage layer, two differently-spelled but path-equivalent relative paths referring to the same physical repository are treated as different limiter keys, splitting one repository's concurrency accounting into multiple buckets.

### Finding Description
`requestinfohandler.extractRequestInfo` sets `info.Repository = targetRepo` straight from the incoming protobuf request via `mi.TargetRepo(reqMsg)`: [1](#0-0) 

`limithandler.LimitConcurrencyByRepo` then uses this unnormalized value as the sole lock key for the per-repo `ConcurrencyLimiter`: [2](#0-1) 

`ConcurrencyLimiter.getConcurrencyLimit`/`Limit` maintain a `map[string]*keyedConcurrencyLimiter` keyed by this exact string, with no normalization applied: [3](#0-2) [4](#0-3) 

Path canonicalization only happens later, deep in the storage stack, e.g. `storage.ValidateRelativePath`, which collapses `..`, trailing slashes, and redundant separators: [5](#0-4) 

Because the limiter middleware runs before this normalization occurs (it operates purely on the string embedded in the request by the gRPC interceptor chain), a client can submit the same repository under multiple distinct raw spellings (e.g. `foo.git/`, `foo.git/./`, `foo.git/sub/..`) and each spelling gets its own independent `max_per_repo`/`max_queue_size` budget in `limitsByKey`, even though they all eventually resolve to the identical on-disk repository. This exactly mirrors the reported bug class: the same underlying resource (a repository, analogous to the staking token) is tracked/accounted in two independent bookkeeping structures (the raw-string-keyed limiter map, analogous to `MasterChef.sol`, vs. the canonicalized on-disk repository, analogous to `ConvexStakingWrapper.sol`/`StakingRewards.sol`), and no mechanism reconciles or collapses the duplicate accounting before the limiter enforces its cap.

### Impact Explanation
An attacker (any client able to issue Gitaly RPCs against a repository they have access to, e.g. via ordinary push/fetch/clone/RPC traffic) can multiply their effective concurrency budget against a single hot repository by simply varying the spelling of the relative path in requests. This defeats the `[[concurrency]]` backpressure mechanism (`max_per_repo`, `max_concurrency`, `max_queue_size`) that is Gitaly's primary defense against resource exhaustion for a given repository/RPC pair, as documented in `doc/backpressure.md`. This can be used to amplify load on a specific repository (I/O, CPU, memory) well beyond configured limits, i.e. a DoS vector against the configured RPC-handler resource limits, consistent with the "Concurrency queue" mitigation being explicitly designed for `max_per_repo=1`-style protections: [6](#0-5) 

### Likelihood Explanation
Exploitability depends on whether GitLab/Rails or the gRPC gateway ever forwards a `relative_path` string in a non-canonical form (trailing slash, `./`, resolvable `..` segments) that a client can influence, and whether such variants pass whatever upstream validation exists before reaching the limiter. This is plausible because the raw string is taken directly from the protobuf request field with no normalization step in the `requestinfohandler`/`limithandler` code path itself; normalization is deferred entirely to the storage layer, which executes only after the limiter has already made its admission decision. I could not fully verify from the indexed code whether Praefect or another upstream hop already canonicalizes `relative_path` before it reaches this limiter, which would reduce likelihood; this should be checked with a live Gitaly session before treating it as immediately exploitable in production.

### Recommendation
Canonicalize the repository's relative path (using the same logic as `storage.ValidateRelativePath`) before computing the concurrency-limiter lock key in `LimitConcurrencyByRepo`, so that all path spellings that resolve to the same physical repository share a single limiter bucket. Alternatively, key the limiter on a resolved, storage-relative canonical identifier (or the repository's assigned partition ID) rather than the raw request string.

### Proof of Concept
1. Configure a `[[concurrency]]` limit, e.g. `max_per_repo = 1`, `max_queue_size = 1`, for a chosen RPC (as shown in `doc/backpressure.md`).
2. Issue two concurrent RPC calls against the same physical repository, but with two different raw `relative_path` strings that normalize to the same path (e.g. `"group/project.git"` and `"group/project.git/"` or `"group/project.git/sub/.."`).
3. Observe that `LimitConcurrencyByRepo` (`internal/grpc/middleware/limithandler/middleware.go:18-25`) produces two different lock keys, so `ConcurrencyLimiter.getConcurrencyLimit` (`internal/limiter/concurrency_limiter.go:245-270`) creates two independent `keyedConcurrencyLimiter` entries, allowing both calls to execute concurrently against the same underlying repository instead of one being queued/rejected as `max_per_repo=1` intends.

### Citations

**File:** internal/grpc/middleware/requestinfohandler/requestinfohandler.go (L204-212)
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
```

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

**File:** internal/limiter/concurrency_limiter.go (L188-212)
```go
// Limit will limit the concurrency of the limited function f. There are two distinct mechanisms
// that limit execution of the function:
//
//  1. First, every call will enter the per-key queue. This queue limits how many callers may try to
//     acquire their per-key semaphore at the same time. If the queue is full the caller will be
//     rejected.
//  2. Second, when the caller has successfully entered the queue, they try to acquire their per-key
//     semaphore. If this takes longer than the maximum queueing limit then the caller will be
//     dequeued and gets an error.
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

**File:** internal/limiter/concurrency_limiter.go (L245-270)
```go
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

**File:** internal/gitaly/storage/locator_test.go (L68-96)
```go
func TestValidateRelativePath(t *testing.T) {
	for _, tc := range []struct {
		path    string
		cleaned string
		error   error
	}{
		{"/parent", "parent", nil},
		{"parent/", "parent", nil},
		{"/parent-with-suffix", "parent-with-suffix", nil},
		{"/subfolder", "subfolder", nil},
		{"subfolder", "subfolder", nil},
		{"subfolder/", "subfolder", nil},
		{"subfolder//", "subfolder", nil},
		{"subfolder/..", ".", nil},
		{"subfolder/../..", "", ErrRelativePathEscapesRoot},
		{"/..", "", ErrRelativePathEscapesRoot},
		{"..", "", ErrRelativePathEscapesRoot},
		{"../", "", ErrRelativePathEscapesRoot},
		{"", ".", nil},
		{".", ".", nil},
	} {
		const parent = "/parent"
		t.Run(parent+" and "+tc.path, func(t *testing.T) {
			cleaned, err := ValidateRelativePath(parent, tc.path)
			assert.Equal(t, tc.cleaned, cleaned)
			assert.Equal(t, tc.error, err)
		})
	}
}
```

**File:** doc/backpressure.md (L15-52)
```markdown
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

For example:

```toml
[[concurrency]]
rpc = "/gitaly.SmartHTTPService/PostUploadPackWithSidechannel"
max_per_repo = 1
max_queue_wait = "1m"
max_queue_size = 5
```
```
