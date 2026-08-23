Based on the pack-objects cache design and implementation, there's a solid gitaly analog to this bug class: an unprivileged fetch client can indefinitely block the packfile-generation pipeline for *other* unrelated fetch clients that happen to share the same cache key, because the cache's backpressure mechanism ties the producer's write progress to the slowest/most malicious consumer's read progress.

### Title
Slow/malicious fetch client can stall shared `git-pack-objects` cache pipe and DoS other fetches for the same packfile - (File: internal/streamcache/pipe.go)

### Summary
`PackObjectsHook` deduplicates concurrent `git-pack-objects` invocations that share the same cache key (repository + args + `want`/`have` stdin) by routing all of them through a single `streamcache` pipe/entry [1](#0-0) . The pipe implements backpressure: the single writer (the running `git-pack-objects` process) blocks on every `Write` call until the *slowest* attached reader has caught up [2](#0-1) . Because multiple unrelated, unprivileged fetch clients can be attached as readers of the same cache entry (`getStream` returns the same pipe reader for cache hits) [3](#0-2) , one client that reads slowly or never reads at all after connecting can stall the packfile generation for every other client sharing that key — analogous to how an unprivileged fee receiver blocks a shared withdrawal path by simply not cooperating with a callback it controls.

### Finding Description
The pack-objects cache is designed so a cache miss spawns a goroutine to run `git-pack-objects`, and the goroutine "may outlive the current request" — it is explicitly documented that a separate goroutine becomes responsible for closing stdin and driving the process for cache hits joining later [1](#0-0) . Once a request produces a cache miss, its `runPackObjects` goroutine continues independent of the originating request's own cancellation (`context.WithoutCancel`) specifically because subsequent joining clients (cache hits) depend on it [4](#0-3) .

The `pipe.Write` backpressure loop blocks the writer whenever `wcursor.Position() > rcursor.Position()`, i.e., whenever any attached reader has fallen behind [2](#0-1) . `rcursor` reflects the position of the *slowest* reader across all consumers of the entry. A reader that opens the sidechannel, matches the same cache key (same repository, same pack-objects args, same `want`/`have` stdin — all attacker-controllable via a normal, unauthenticated-content fetch/clone request), and then simply stops reading (or reads at a negligible rate) will keep `rcursor` pinned, which blocks the shared writer from making further progress. Any other legitimate client that is a cache hit on the same key is attached to the very same pipe and stalls identically until the slow reader disconnects, is evicted, or a timeout intervenes.

This exact hazard is acknowledged in the design documentation: "`git-pack-objects` processes may remain in a waiting state until the fastest client requesting the same cache key completes its operation. During this waiting period, these processes can occupy substantial server resources." [5](#0-4)  The documentation frames this only as a resource-occupation side effect and recommends disabling backpressure as a mitigation, but does not address the case where the slow/non-reading party is an unprivileged, adversarial client deliberately triggering a predictable cache key (e.g., requesting a fetch of a known ref set) to hold the shared writer hostage and deny service to other tenants requesting the same object range.

### Impact Explanation
An unauthenticated/low-privilege client that can issue `PostUploadPack`/`SSHUploadPack`/clone-style fetch requests (which route to `PackObjectsHookWithSidechannel`) can, without any special access, cause `git-pack-objects` for a given repository/ref-set to hang indefinitely while consuming a CPU/memory/concurrency-limiter slot, and simultaneously stall every other client whose request maps to the same cache key. This is a resource-exhaustion / DoS of the fetch/clone/pull path for other unprivileged users of a public or shared repository (e.g., CI runners fetching the same commit range concurrently), which was the intended use case that motivated building this cache in the first place.

### Likelihood Explanation
Likelihood is high in environments where the pack-objects cache and backpressure are enabled (the documented default) and CI-style parallel fetches are common — exactly the scenario the feature was built for. An attacker only needs to open the fetch connection, negotiate a `want`/`have` set matching a target victim's expected request (predictable for popular refs/commits), and then stop reading the response stream (e.g., not calling `Recv` further, or intentionally slow-reading via a throttled connection), which is trivial to do from any client with fetch access.

### Recommendation
Enforce a read-progress/idle timeout per reader attached to a `streamcache` pipe entry so that a stalled or adversarial reader is forcibly detached (and its slot released) after a bounded period rather than being allowed to block the shared writer indefinitely. Alternatively/additionally, decouple `rcursor` from the single slowest reader (e.g., use a per-reader bounded buffer so one slow reader cannot regress backpressure for others), and ensure `PackObjectsHookWithSidechannel` enforces a minimum read-rate or overall deadline on the response side regardless of cache-hit/miss status.

### Proof of Concept
1. Client A issues `PostUploadPackWithSidechannel`/`SSHUploadPackWithSidechannel` for repository R with `want`/`have` set W, causing a cache miss and starting `git-pack-objects`; A reads one byte of the sideband response then stops reading (never closes/cancels the gRPC stream) — mirrored by the test harness pattern in `TestServer_PackObjectsHook_separateContext`, which shows two clients attaching to the same cache entry and demonstrates the shared-writer coupling [6](#0-5) .
2. Client B (legitimate, unprivileged) issues the same fetch for R with the same W, producing a cache hit that attaches as a second reader to the same pipe via `getStream` [3](#0-2) .
3. Because A stops reading, `rcursor` stalls at A's last-read position; `pipe.Write` blocks the shared `git-pack-objects` writer per the backpressure loop [2](#0-1) , so B never receives further packfile data despite B actively reading, until A's connection is torn down (e.g., by gRPC-level timeout) or the entry is evicted.

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

**File:** internal/streamcache/pipe.go (L105-124)
```go
func (p *pipe) Write(b []byte) (int, error) {
	// Loop (block) until at least one reader catches up with our last write.
	if p.backpressure {
		for p.wcursor.Position() > p.rcursor.Position() {
			select {
			case <-p.wcursor.Done():
				// Prevent writing bytes no-one will read
				return 0, errWrongCloseOrder
			case <-p.wnotifier.C:
			}
		}
	} else {
		// Even though disabling backpressure allows the writer to write data without waiting for readers, it
		// does not make sense to continue writing if nobody is interested in the result anymore.
		select {
		case <-p.wcursor.Done():
			return 0, errWrongCloseOrder
		default:
		}
	}
```

**File:** internal/streamcache/cache.go (L235-260)
```go
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

**File:** doc/design_pack_objects_cache.md (L100-109)
```markdown
While the backpressure mechanism provides the benefits described above, it can have a side effect:
`git-pack-objects` processes may remain in a waiting state until the fastest client requesting the
same cache key completes its operation. During this waiting period, these processes can occupy
substantial server resources.

Consider disabling backpressure if you observe:

- Hung `git-pack-objects` processes occupying excessive server resources
- Memory pressure during heavy load scenarios
- Slower overall performance with backpressure enabled
```

**File:** internal/gitaly/service/hook/pack_objects_test.go (L130-191)
```go
	// The first call sends a valid request, but will then immediately hang up without reading the response. This
	// should not impact the second call in any way even if it uses the same cache entry.
	wg.Add(1)
	go func() {
		defer wg.Done()

		ctx, cancel := context.WithCancel(ctx)
		defer cancel()

		client, conn := newHooksClient(t, cfg.SocketPath)
		defer testhelper.MustClose(t, conn)

		ctx, wt, err := hookPkg.SetupSidechannel(
			ctx,
			gitcmd.HooksPayload{
				RuntimeDir: runtimeDir,
			},
			func(c *net.UnixConn) error {
				if _, err := io.WriteString(c, stdin); err != nil {
					return err
				}
				if err := c.CloseWrite(); err != nil {
					return err
				}

				// Read one byte of the response to ensure that this call got handled before the next
				// one. Afterwards we exit immediately without reading the rest of the response.
				buf := make([]byte, 1)
				_, err := io.ReadFull(c, buf)

				// Step 2: unblock the second Goroutine such that it can start invoking the RPC. At this
				// point in time we know that git-pack-objects(1) is running already and originally
				// created by this Goroutine.
				syncCh <- struct{}{}
				// Step 3: we wait for the second Goroutine to catch up and end up in the code that
				// handles the sidechannel.
				<-syncCh

				return err
			},
		)
		require.NoError(t, err)
		defer testhelper.MustClose(t, wt)

		_, err = client.PackObjectsHookWithSidechannel(ctx, req)
		if runtime.GOOS == "darwin" {
			require.Error(t, err)
			// macOS uses different logic than Linux systems because the sendfile(3P) syscall is not
			// available. The resulting error message is non-deterministic and includes the actual path of
			// the pipe we're trying to write to.
			require.Regexp(t, "pack objects hook: write unix ->.*: write: broken pipe", err.Error())
			testhelper.RequireGrpcCode(t, err, codes.Canceled)
		} else {
			testhelper.RequireGrpcError(t, structerr.NewCanceled("pack objects hook: broken pipe"), err)
		}
		require.NoError(t, wt.Wait())

		cancel()

		// Step 6: unblock the second Goroutine such that it can resume processing the git-pack-objects(1) data.
		syncCh <- struct{}{}
	}()
```
