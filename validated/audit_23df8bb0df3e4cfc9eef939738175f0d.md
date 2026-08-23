### Title
Unconditional cache-entry deletion after external `git-pack-objects` callback races with concurrent creators, evicting a valid in-flight entry - (File: internal/streamcache/cache.go)

### Summary
`streamcache.cache.newEntry()` spawns a goroutine that runs the caller-supplied `create` callback (in practice `git-pack-objects`, an external process invoked from `PackObjectsHook`) and, if it fails, deletes the cache's `index[key]` entry unconditionally instead of verifying that the entry still belongs to the failed attempt. This mirrors the reported Solidity bug class: state is mutated ("delete balance"/"delete cache index") *after* an external call returns, without re-checking that the state still corresponds to what the goroutine originally owned, so a concurrent operation that runs during the external call's "reentrant" window can be clobbered.

### Finding Description
`newEntry` builds an `entry` `e`, stores `c.index[key] = e` under `c.m` in `getStream`, and starts a background goroutine that calls `runCreate(e.pipe, create)` — which invokes `git-pack-objects` via `gitCmdFactory.New(...).Wait()`, an external subprocess call that can run for an extended, attacker-influenceable time [1](#0-0) . On failure, the goroutine takes `c.m.Lock()` and calls `c.delete(key)` with no check that `c.index[key]` is still `e`: [2](#0-1) 

Compare this with `c.getStream`, which, when it finds a stale/broken entry, also calls the same unconditional `c.delete(key)` before installing a fresh entry [3](#0-2) :

Sequence:
1. Request A's `Fetch(key)` misses, creates entry `e1`, stores `index[key] = e1`, and its `create()` (git-pack-objects) is slow/about to fail (e.g., the requested objects are transient, get GC'd, or the process is killed by resource limits enforced in `runPackObjectsLimited`).
2. Before `e1`'s failure-handling goroutine acquires `c.m`, a concurrent Request B computes the identical cache key (same repository/args/stdin hash, see `computeCacheKey`) and calls `getStream`. It finds `index[key] == e1`, tries `e1.pipe.OpenReader()`, which fails because `e1`'s pipe is already broken/closing, so B calls `c.delete(key)` and installs a brand-new entry `e2` (`index[key] = e2`), spawning goroutine G2 to run `create()` again.
3. G1 (from step 1) now acquires the lock and calls `c.delete(key)` unconditionally, deleting `e2` — B's still in-flight, otherwise-valid cache entry — from the index, even though G1 has no relationship to `e2`.

This is exactly the "external call → state mutation without re-validating ownership" pattern from the report: the code that runs after the external call (`create`) blindly clears shared keyed state that may have already been legitimately reassigned to a different, concurrent operation.

### Impact Explanation
Because `c.index` is the only place `clean()` looks when evicting/removing on-disk cache files, once `e2` is silently unlinked from `index[key]`, the periodic `clean()` sweep will never call `e2.pipe.RemoveFile()` for it (it iterates `c.index`, from which `e2` is now absent) [4](#0-3) . Subsequent `Fetch(key)` callers see a cache miss and will spin up yet another `git-pack-objects` invocation (`e3`), duplicating CPU/IO work that the pack-objects cache exists specifically to avoid, since it's designed to protect a Gitaly server from "high...CPU load due to massively parallel CI fetches" [5](#0-4) . This is reachable purely from an ordinary user's `git fetch`/`git clone` traffic through `PackObjectsHookWithSidechannel`, since `packObjectsHook` derives the cache key from the (public) request parameters and stdin and calls into `s.packObjectsCache.Fetch` with the `create` callback wired to `runPackObjects`/`runPackObjectsLimited` [6](#0-5) [7](#0-6) . The net effect is a repeated cache-defeat/self-DoS amplification against the intended purpose of the pack-objects cache (increased server CPU/IO under many concurrent identical fetches), not an object-corruption or cross-repository data-exposure bug: orphaned on-disk files are still eventually reclaimed by the separate filestore directory-walk cleanup goroutine mentioned in the package doc comment, limiting this to a resource-efficiency/DoS-adjacent issue rather than unbounded disk exhaustion.

### Likelihood Explanation
Triggering requires winning a narrow race: an attacker (or even accidental legitimate concurrent traffic) needs two `PackObjectsHookWithSidechannel` calls with the *same* computed cache key (same repository, same pack-objects arguments, same stdin/"wants"+"haves") where the first's `create()` fails or its pipe becomes unreadable, timed so the second request's `getStream` reopens/reallocates the entry before the first's failure-handling goroutine takes the lock. This is plausible under realistic conditions (e.g., many CI runners fetching the same commit concurrently, one of which is cancelled/times out or hits `runPackObjectsLimited`'s concurrency limiter causing an early error) but is a timing-dependent race rather than a deterministic, single-request exploit, so likelihood is moderate rather than high.

### Recommendation
In the failure branch inside `newEntry`'s goroutine, verify identity before deleting, exactly as already done correctly elsewhere in the same file (`run`/`evictIfDone` in `keyed_runner_cache.go` use `c.items[key] == item` before deleting [8](#0-7) ):
```go
c.m.Lock()
if c.index[key] == e {
    c.delete(key)
}
c.m.Unlock()
```
This applies the same checks-effects pattern recommended in the source report (validate that the state still belongs to the caller before mutating it), rather than deleting the map entry unconditionally after the external process call returns.

### Proof of Concept
1. Enable the pack-objects cache (`cfg.PackObjectsCache.Enabled = true`).
2. Issue Fetch/Clone request A for a set of "want"/"have" refs against a repository, but arrange for `git-pack-objects` to fail after entry creation (e.g., invalidate the requested objects mid-flight, or trigger `runPackObjectsLimited`'s per-IP concurrency limit to make the first attempt error out) so `newEntry`'s background goroutine reaches the `err != nil` branch in [2](#0-1) .
3. Concurrently, before that goroutine acquires `c.m`, issue Fetch/Clone request B with identical cache-key–producing parameters (same repo/args/stdin) so `getStream` observes the broken pipe from A's entry, deletes it, and installs a new entry `e2`, per [9](#0-8) .
4. Let A's failure-handling goroutine proceed to call `c.delete(key)`, removing B's `e2` from `c.index` while B's `git-pack-objects` process is still running.
5. Observe that a subsequent Fetch/Clone with the same key experiences a cache miss and re-triggers `git-pack-objects` even though B's generation was never actually interrupted — confirmable via the `pack_objects_cache_lookups_total{result="miss"}` metric incrementing unexpectedly for repeat identical requests, and via `clean()`'s inability to ever remove B's orphaned on-disk pipe file since it is absent from `c.index`.

### Citations

**File:** internal/streamcache/cache.go (L177-204)
```go
func (c *cache) clean() {
	c.m.Lock()
	defer c.m.Unlock()

	var removed []*entry
	cutoff := time.Now().Add(-c.maxAge)
	for k, e := range c.index {
		if e.created.Before(cutoff) {
			c.delete(k)
			removed = append(removed, e)
		}
	}

	// Batch together file removals in a goroutine, without holding the mutex
	go func() {
		for _, e := range removed {
			if err := e.pipe.RemoveFile(); err != nil && !os.IsNotExist(err) {
				c.logger.WithError(err).Error("streamcache: remove file evicted from index")
			}
		}

		if c.removalCond != nil {
			c.removalCond.L.Lock()
			defer c.removalCond.L.Unlock()
			c.removalCond.Broadcast()
		}
	}()
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

**File:** doc/design_pack_objects_cache.md (L47-59)
```markdown
## Problem scope

We designed this cache to solve a specific problem on GitLab.com: high
Gitaly server CPU load due to massively parallel CI fetches.

That means:

1. It was OK if some types of fetch traffic did not become faster, as long as they also did not get slower
1. It was OK to make specific assumptions about the infrastructure this runs on

Example for (1): we made sure the cache can stream unfinished responses because without that, cache misses would be noticeably slower.

Example for (2): GitLab.com uses 16TB filesystems with at least 2TB of free space to store repositories. If our cache files are on there, and as long as the average size is reasonable, we don't have to worry about peak cache size. The worst case average cache size we projected was 30GB, which is just 1.5% of the 2TB of expected free space.
```

**File:** internal/gitaly/service/hook/pack_objects.go (L47-90)
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
	if err != nil {
		return err
	}
```

**File:** internal/gitaly/service/hook/pack_objects.go (L214-259)
```go
func runPackObjects(
	ctx context.Context,
	gitCmdFactory gitcmd.CommandFactory,
	w io.Writer,
	req *gitalypb.PackObjectsHookWithSidechannelRequest,
	args *packObjectsArgs,
	stdin io.Reader,
	key string,
) error {
	repo := req.GetRepository()

	counter := &helper.CountingWriter{W: w}
	sw := pktline.NewSidebandWriter(counter)
	stdout := bufio.NewWriterSize(sw.Writer(stream.BandStdout), pktline.MaxSidebandData)
	stderrBuf := &bytes.Buffer{}
	stderr := io.MultiWriter(sw.Writer(stream.BandStderr), stderrBuf)

	defer func() {
		packObjectsGeneratedBytes.Add(float64(counter.N))
		customFields := log.CustomFieldsFromContext(ctx)
		if customFields != nil {
			customFields.RecordMetadata("pack_objects_cache.key", key)
			customFields.RecordSum("pack_objects_cache.generated_bytes", int(counter.N))
			if total := totalMessage(stderrBuf.Bytes()); total != "" {
				customFields.RecordMetadata("pack_objects.compression_statistics", total)
			}
		}
	}()

	cmd, err := gitCmdFactory.New(ctx, repo, args.subcmd(),
		gitcmd.WithStdin(stdin),
		gitcmd.WithStdout(stdout),
		gitcmd.WithStderr(stderr),
		gitcmd.WithGlobalOption(args.globals()...),
	)
	if err != nil {
		return err
	}
	if err := cmd.Wait(); err != nil {
		return fmt.Errorf("git-pack-objects: stderr: %q err: %w", stderrBuf.String(), err)
	}
	if err := stdout.Flush(); err != nil {
		return fmt.Errorf("flush stdout: %w", err)
	}
	return nil
}
```

**File:** internal/git/mvcc/keyed_runner_cache.go (L139-152)
```go
	c.mu.Lock()
	item.err = err
	item.completed = true
	if err != nil {
		// Discard the failed entry so that subsequent callers start a fresh
		// execution rather than reusing the failure. Callers already attached
		// still have a reference to the item so it is safe to delete.
		if c.items[key] == item {
			delete(c.items, key)
		}
	} else {
		c.evictIfDone(key, item)
	}
	c.mu.Unlock()
```
