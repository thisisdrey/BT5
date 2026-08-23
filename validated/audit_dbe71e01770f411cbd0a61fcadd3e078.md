### Title
Single Failed `git-pack-objects` Execution in the Pack-Objects Cache Causes Denial of Service for All Concurrent Requesters Sharing the Same Cache Key - (File: internal/streamcache/cache.go, internal/gitaly/service/hook/pack_objects.go)

### Summary
The `PackObjectsHookWithSidechannel` RPC coalesces concurrent, identical `git-upload-pack` object-negotiation requests (fetch/clone/pull) into a single shared `git-pack-objects` execution keyed by a hash of the repository, arguments, and negotiated stdin. All concurrent callers that hash to the same key "join" this single execution and wait on a shared `waiter`. If that one execution fails, the failure is broadcast verbatim to every waiting caller — including legitimate, unrelated users whose own negotiated fetch would otherwise have succeeded independently. This mirrors the reported bug class: a shared, atomic batch execution spanning multiple independent actors where one failure aborts the outcome for all of them.

### Finding Description
`packObjectsHook` computes a cache key from the repository, `pack-objects` args, git protocol, and the negotiated stdin (the `wants`/`haves` list), then calls `s.packObjectsCache.Fetch`, which coalesces concurrent identical requests: [1](#0-0) 

The cache key derivation includes only the repository, request args and protocol, and hashed stdin content — nothing that ties the entry to a specific caller/session: [2](#0-1) 

In `streamcache.cache`, `Fetch`/`getStream` looks up an existing in-flight `entry` for the key and, if found, simply attaches the new caller as another reader/waiter of the *same* underlying execution rather than starting an independent one: [3](#0-2) 

The actual `git-pack-objects` invocation happens once per key in a background goroutine (`newEntry`), and its single error result is recorded on a shared `waiter` that unblocks and returns the *same* error to every caller waiting on that key: [4](#0-3) [5](#0-4) 

The comment in `runPackObjects` explicitly acknowledges that multiple unrelated clients ("client1" and "client2") can share one execution, and the code goes out of its way to isolate *cancellation* propagation between them via `context.WithoutCancel` — but it does not isolate *failure* propagation: [6](#0-5) 

The design document for this cache confirms the intended behavior is to deduplicate identical concurrent fetches (e.g. CI pipelines fetching the same ref) into one execution: [7](#0-6) 

Consequently, exactly like the `quexCallback()` pattern in the external report — where many independent users' deposit/withdrawal requests are bundled into one all-or-nothing transaction and one blacklisted/malicious recipient can revert the whole batch — this cache bundles many independent users' identical pack-objects requests into one all-or-nothing execution, and a single failure (crash, resource exhaustion, unexpected git error, disk failure, panic recovered as an error in `runCreate`) is propagated as the result for every concurrent legitimate requester of the same objects.

### Impact Explanation
Any concurrent, unrelated client (e.g., CI runners or multiple users fetching the same popular branch/tag at the same time — the scenario this cache is explicitly optimized for per the design doc) that happens to negotiate an identical `wants`/`haves` set with `git-upload-pack` will be silently attached to the same cache entry. If that single shared `git-pack-objects` execution fails for any reason (including reasons triggered or influenced by one specific participant, such as resource limits, disk pressure from `bufferStdin`'s temp files, or an unexpected git error), every other legitimate fetch/clone sharing that key fails too, denying service to users whose own negotiation was otherwise valid. This is a DoS of an RPC handler (`PackObjectsHookWithSidechannelRequest`) reachable from ordinary git fetch/clone/pull traffic.

### Likelihood Explanation
Exploitability depends on achieving a cache-key collision with a victim's in-flight request, which requires negotiating the identical `wants`/`haves` set, arguments, and protocol — most likely to occur naturally (and thus be triggerable somewhat predictably by an attacker who also fetches a known popular ref concurrently with other users) in exactly the traffic patterns the cache is built for: CI pipelines and popular branches/tags with many simultaneous identical fetches. The feature is opt-in (`pack_objects_cache.enabled`), so exposure is limited to deployments that enable it.

### Recommendation
Do not propagate a single execution's failure as a hard failure to every waiting consumer. Consider: (1) isolating failure domains per logical request rather than only per exact cache key, (2) retrying/falling back to an independent `git-pack-objects` execution for followers when the shared leader execution fails instead of returning the same error to all waiters, and (3) capping/rate-limiting how many followers can attach to a single in-flight entry so a targeted failure has bounded blast radius.

### Proof of Concept
1. Enable `pack_objects_cache` on a Gitaly node serving a public repository with a popular ref.
2. Have two independent git clients (simulating two unrelated users) issue `git fetch`/`git clone` requests that negotiate to the exact same `wants`/`haves` set against the same ref (e.g., both doing a fresh clone at the same moment), so both hit `PackObjectsHookWithSidechannel` with identical `Repository`/`Args`/`GitProtocol`/stdin, producing the same `computeCacheKey` result (`internal/gitaly/service/hook/pack_objects.go:117-145`).
3. Ensure client A's request is first (`getStream` creates the entry, `internal/streamcache/cache.go:235-260`), causing client B to attach as a waiter on the same `entry.waiter` (`internal/streamcache/cache.go:346-368`).
4. Induce (or observe) a failure in the single backing `git-pack-objects` invocation for client A's execution (e.g., disk exhaustion of the cache/temp storage, or any git-pack-objects runtime error captured in `runCreate`, `internal/streamcache/cache.go:270-321,323-344`).
5. Observe that client B's independently-valid fetch also fails with the identical error via `wt.Wait(ctx)` in `Fetch` (`internal/streamcache/cache.go:215-233`), even though client B's own negotiation and request were valid and unrelated to the cause of failure.

### Citations

**File:** internal/gitaly/service/hook/pack_objects.go (L47-65)
```go
func (s *server) packObjectsHook(ctx context.Context, req *gitalypb.PackObjectsHookWithSidechannelRequest, args *packObjectsArgs, stdinReader io.Reader, output io.Writer) error {
	cacheKey, stdin, err := s.computeCacheKey(ctx, req, stdinReader)
	if err != nil {
		return err
	}

	// We do not know yet who has to close stdin. In case of a cache hit, it
	// is us. In case of a cache miss, a separate goroutine will run
	// git-pack-objects, and that goroutine may outlive the current request.
	// In that case, that separate goroutine will be responsible for closing
	// stdin.
	closeStdin := true
	defer func() {
		if closeStdin {
			stdin.Close()
		}
	}()

	servedBytes, created, err := s.packObjectsCache.Fetch(ctx, cacheKey, output, func(w io.Writer) error {
```

**File:** internal/gitaly/service/hook/pack_objects.go (L117-145)
```go
func (s *server) computeCacheKey(ctx context.Context, req *gitalypb.PackObjectsHookWithSidechannelRequest, stdinReader io.Reader) (string, io.ReadCloser, error) {
	cacheHash := sha256.New()

	repository := req.GetRepository()
	if tx := storage.ExtractTransaction(ctx); tx != nil {
		// The cache uses the requests as the keys. As the request's repository in the RPC handler has been rewritten
		// to point to the transaction's repository, the handler sees each request as different even if they point to
		// the same repository. Restore the original request to ensure identical requests get the same key.
		repository = tx.OriginalRepository(req.GetRepository())
	}

	cacheKeyPrefix, err := protojson.Marshal(&gitalypb.PackObjectsHookWithSidechannelRequest{
		Repository:  repository,
		Args:        req.GetArgs(),
		GitProtocol: req.GetGitProtocol(),
	})
	if err != nil {
		return "", nil, err
	}
	if _, err := cacheHash.Write(cacheKeyPrefix); err != nil {
		return "", nil, err
	}
	stdin, err := bufferStdin(stdinReader, cacheHash)
	if err != nil {
		return "", nil, err
	}
	cacheKey := hex.EncodeToString(cacheHash.Sum(nil))
	return cacheKey, stdin, nil
}
```

**File:** internal/gitaly/service/hook/pack_objects.go (L147-174)
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
```

**File:** internal/streamcache/cache.go (L215-260)
```go
func (c *cache) Fetch(ctx context.Context, key string, dst io.Writer, create func(io.Writer) error) (written int64, created bool, err error) {
	var (
		rc io.ReadCloser
		wt *waiter
	)
	rc, wt, created, err = c.getStream(key, create)
	if err != nil {
		return written, created, err
	}
	defer rc.Close()

	written, err = io.Copy(dst, rc)
	if err != nil {
		return written, created, err
	}

	err = wt.Wait(ctx)
	return written, created, err
}

func (c *cache) getStream(key string, create func(io.Writer) error) (_ io.ReadCloser, _ *waiter, created bool, err error) {
	c.m.Lock()
	defer c.m.Unlock()

	if e := c.index[key]; e != nil {
		if r, err := e.pipe.OpenReader(); err == nil {
			return r, e.waiter, false, nil
		}

		// In this case err != nil. That is allowed to happen, for instance if
		// the *filestore cleanup goroutine deleted the file already. But let's
		// remove the key from the cache to save the next caller the effort of
		// trying to open this entry.
		c.delete(key)
	}

	r, e, err := c.newEntry(key, create)
	if err != nil {
		return nil, nil, false, err
	}

	c.index[key] = e
	c.setIndexSize()

	return r, e.waiter, true, nil
}
```

**File:** internal/streamcache/cache.go (L270-321)
```go
func (c *cache) newEntry(key string, create func(io.Writer) error) (_ io.ReadCloser, _ *entry, err error) {
	e := &entry{
		key:     key,
		cache:   c,
		created: time.Now(),
		waiter:  newWaiter(),
	}

	// Every entry gets a unique underlying file. We do not want to reuse
	// existing cache files because we do not know whether they are the
	// result of a successful call to create.
	//
	// This may sound like we should be using an anonymous tempfile, but that
	// would be at odds with the requirement to be able to open and close
	// multiple instances of the file independently: one for the writer, and
	// one for each reader.
	//
	// So the name of the file is irrelevant, but the file must have _a_
	// name.
	f, err := c.createFile()
	if err != nil {
		return nil, nil, err
	}
	defer func() {
		if err != nil {
			f.Close()
		}
	}()

	var pr io.ReadCloser
	pr, e.pipe, err = newPipe(f, c.backpressure)
	if err != nil {
		return nil, nil, err
	}

	go func() {
		err := runCreate(e.pipe, create)

		// We defer this until after we have removed the cache entry so that the waiter is
		// only unblocked when the cache key has already been pruned from the cache.
		defer e.waiter.SetError(err)

		if err != nil {
			c.logger.WithError(err).Error("create cache entry")
			c.m.Lock()
			defer c.m.Unlock()
			c.delete(key)
		}
	}()

	return pr, e, nil
}
```

**File:** internal/streamcache/cache.go (L346-368)
```go
type waiter struct {
	done chan struct{}
	err  error
	once sync.Once
}

func newWaiter() *waiter { return &waiter{done: make(chan struct{})} }

func (w *waiter) SetError(err error) {
	w.once.Do(func() {
		w.err = err
		close(w.done)
	})
}

func (w *waiter) Wait(ctx context.Context) error {
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-w.done:
		return w.err
	}
}
```

**File:** doc/design_pack_objects_cache.md (L100-113)
```markdown
While the backpressure mechanism provides the benefits described above, it can have a side effect:
`git-pack-objects` processes may remain in a waiting state until the fastest client requesting the
same cache key completes its operation. During this waiting period, these processes can occupy
substantial server resources.

Consider disabling backpressure if you observe:

- Hung `git-pack-objects` processes occupying excessive server resources
- Memory pressure during heavy load scenarios
- Slower overall performance with backpressure enabled

When backpressure is disabled, Gitaly will write to the cache at its maximum rate regardless of
client consumption, which may increase I/O but could improve throughput in certain environments,
especially when there are many concurrent fetches of the same data.
```
