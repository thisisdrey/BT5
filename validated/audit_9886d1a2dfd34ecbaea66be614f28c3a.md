### Title
Unrecovered panic in bundle-URI generation worker goroutine can crash the entire Gitaly process - ([File: internal/bundleuri/strategy_occurences.go])

### Summary
`OccurrenceStrategy.startGenerationWorkers` runs `request.cb(request.ctx, request.repo)` directly inside a bare `go func()` with no `recover()`, unlike the codebase's own convention of using `dontpanic.Go`/`dontpanic.Try` for exactly this kind of "fire-and-forget" background goroutine. Since this worker pool is started once at server startup and is shared across all repositories and all gRPC connections, any panic inside the callback chain (`GenerationManager.Generate` → `Sink.getWriter`/`repo.CreateBundle`) is outside the reach of `panichandler.UnaryPanicHandler`/`StreamPanicHandler`, which are only installed around the synchronous RPC call stack in `internal/gitaly/server/server.go`.

### Finding Description
An unprivileged user can trigger `git fetch`/`clone` repeatedly on a repository they own. `internal/gitaly/service/smarthttp/upload_pack.go` calls `s.bundleURIManager.GenerateWithStrategy`, which calls `OccurrenceStrategy.Evaluate` [1](#0-0) . Once the occurrence threshold is met, the request is pushed onto `generateQueue` and consumed asynchronously by one of the `maxConcurrent` goroutines started in `startGenerationWorkers`: [2](#0-1) 

Note that `request.cb(request.ctx, request.repo)` is invoked with **no panic recovery** at all — no `recover()`, no `dontpanic.Try`/`dontpanic.Go` wrapper — even though the codebase has an established `internal/dontpanic` package specifically intended for exactly this class of goroutine ("fire-and-forget goroutines where observability is lost") [3](#0-2) .

The callback `cb` resolves to `GenerationManager.Generate`, which performs several operations before/while writing to the bundle sink, including `repo.HeadReference`, transaction/storage lookups, `g.sink.getWriter`, and `repo.CreateBundle` [4](#0-3) . `Sink.getWriter` itself wraps `s.bucket.NewWriter` and converts errors into returned `error` values rather than panicking under normal conditions [5](#0-4) .

Because this whole worker loop runs in a goroutine spawned by `Start(ctx)` at Gitaly server startup (not per-RPC), it lives completely outside the boundary that `panichandler.UnaryPanicHandler`/`StreamPanicHandler` protects — those interceptors only wrap the top-level RPC handler invocation, and Go panics in a goroutine cannot be recovered by a `defer`/`recover()` in a different goroutine's call stack. A panic here is therefore fatal to the whole process: `panic: ...` followed by `runtime.Goexit()`/process termination, taking down every repository served by that Gitaly node, not just the attacker's own repository. This matches the audit's stated invariant violation: "a single unauthorized/malformed input in one repository's requests must not corrupt or crash server-wide state outside the RPC's own error boundary."

I was not able to find a concrete, currently-reachable panic-inducing code path inside `Generate`/`getWriter` in the version of the code I inspected — the functions I reviewed (`repo.HeadReference`, `Sink.getWriter`, error handling in `Generate`) return errors rather than panicking for the failure modes I could trace (e.g., missing HEAD, bucket write errors, nil transaction lookups all return early with logged errors). The `errors.Unwrap` call in `Sink.signedURL` and other defensive checks (`g.sink == nil` is rejected at construction time in `NewGenerationManager`) reduce the likelihood of an easily reachable nil-pointer panic today. So while the missing panic-recovery is a genuine, real defect, I could not confirm a specific attacker-triggerable panic trigger within the reachable code paths using static analysis alone — this would need dynamic fuzz-testing of `CreateBundle`/`blob.Bucket` driver internals (e.g. `gocloud.dev/blob`) or future code changes that introduce a panic in this call chain.

### Impact Explanation
If any panic (current or future, in this callback chain or any hook/library it depends on) fires inside this goroutine, it crashes the entire `gitaly` process, causing denial of service for **all** repositories and RPCs served by that node — not just the attacker-controlled repository. This matches GitLab's "Denial of Service" bounty impact class at a severity elevated by the fact that the blast radius is server-wide rather than scoped to the single request/repo, which is the specific defect: the architecture provides no isolation/containment for panics in this background execution path, unlike the RPC path which is explicitly protected by `panichandler`.

### Likelihood Explanation
Reaching the vulnerable code path requires no special privileges — merely triggering enough fetch/clone occurrences (`threshold`, `interval` from bundle-URI strategy config) against a repository the attacker controls to get `OccurrenceStrategy.evaluate` to return true and enqueue a generation request [6](#0-5) , which is fully repeatable and requires no privileged access. However, actually **triggering a panic** requires an as-yet-unidentified bug in `Generate`/`getWriter`/`CreateBundle`/the `gocloud.dev/blob` driver, since current code paths I reviewed return errors defensively rather than panicking. Likelihood of the containment gap being exploited today is therefore speculative/unconfirmed, though the missing-recovery defect itself is concretely present and verifiable by code inspection.

### Recommendation
Wrap the callback invocation in `startGenerationWorkers` with `dontpanic.Try` (or an equivalent local `recover()`), consistent with the pattern already established in `internal/dontpanic`, e.g.:
```go
case request := <-s.generateQueue:
    var err error
    dontpanic.Try(s.logger, func() {
        err = request.cb(request.ctx, request.repo)
    })
    ...
```
This ensures a panic in bundle generation for one repository is logged/reported to Sentry and only fails that single generation attempt, instead of crashing the entire Gitaly process and every other repository it serves.

### Proof of Concept
```go
// internal/bundleuri/strategy_occurences_panic_test.go
func TestStartGenerationWorkers_PanicNotRecovered(t *testing.T) {
    s, err := NewOccurrenceStrategy(testLogger(t), 2, time.Minute, 1, 0)
    require.NoError(t, err)

    ctx, cancel := context.WithCancel(context.Background())
    defer cancel()
    stop := s.Start(ctx)
    defer stop()

    panicking := evaluateRequest{
        ctx: ctx,
        cb: func(ctx context.Context, repo *localrepo.Repo) error {
            panic("simulated panic in bundle generation callback")
        },
    }

    // Directly push onto generateQueue to simulate what `process()` does
    // once evaluate() decides a bundle should be generated.
    s.generateQueue <- panicking

    // Expected (buggy) behavior: the worker goroutine panics and crashes
    // the whole test binary/process because there is no recover() in
    // startGenerationWorkers. A fixed implementation using dontpanic.Try
    // would instead let this test complete normally.
    time.Sleep(100 * time.Millisecond)
}
```
Running this test with `go test -run TestStartGenerationWorkers_PanicNotRecovered` demonstrates that the panic is not recovered and terminates the test binary, confirming that in production this same code path would crash the `gitaly` process.

### Citations

**File:** internal/bundleuri/manager.go (L85-90)
```go
func (g *GenerationManager) GenerateWithStrategy(ctx context.Context, repo *localrepo.Repo) error {
	if featureflag.BundleGeneration.IsEnabled(ctx) {
		return g.strategy.Evaluate(ctx, repo, g.Generate)
	}
	return nil
}
```

**File:** internal/bundleuri/manager.go (L94-176)
```go
func (g *GenerationManager) Generate(ctx context.Context, repo *localrepo.Repo) (returnErr error) {
	ref, err := repo.HeadReference(ctx)
	if err != nil {
		return fmt.Errorf("resolve HEAD ref: %w", err)
	}

	repoProto, ok := repo.Repository.(*gitalypb.Repository)
	if !ok {
		return fmt.Errorf("unexpected repository type %t", repo.Repository)
	}

	// We need a distinct context from the request's context (ctx).
	// This is because if we use the request's context during bundle generation,
	// and if this request runs inside a transaction, the snapshot that holds a
	// copy of the repo will be deleted at the end of the transaction, but the bundle
	// might not have finished generating yet. So we need a new context, and a new
	// transaction inside that context, so we can have a snapshot that holds for
	// the duration of the bundle generation. We also want this new context to be
	// `from` the manager's context to inherit its cancellation.
	gCtx, cancel := context.WithCancel(g.ctx)
	defer cancel()

	// We must use `ctx` here and not `gCtx`, because `ctx` is the context
	// of the gRPC request, and this is what we want.
	bundlePath := bundleRelativePath(ctx, repo, defaultBundle)
	if tx := storage.ExtractTransaction(ctx); tx != nil {
		if g.nodeManager == nil {
			g.logger.WithError(err).Error("generate bundle: nil node manager within transaction")
			return nil
		}

		originalRepo := tx.OriginalRepository(repoProto)
		strg, err := g.nodeManager.GetStorage(originalRepo.GetStorageName())
		if err != nil {
			g.logger.WithError(err).Error("generate bundle: error getting storage")
			return nil
		}
		// Create the transaction on the new context created above
		ntx, err := strg.Begin(gCtx, storage.TransactionOptions{
			ReadOnly:     true,
			RelativePath: originalRepo.GetRelativePath(),
		})
		if err != nil {
			g.logger.WithError(err).Error("generate bundle: no transaction found")
			return nil
		}

		// We only create a new transaction to have a dedicated snapshot during
		// bundle generation. So once the bundle is generated, we must abort
		// to free the snapshot.
		defer func() { _ = ntx.Rollback(gCtx) }()

		// We must use `ctx` here and not `gCtx`, because `ctx` is the context
		// of the gRPC request, and this is what we want.
		bundlePath = bundleRelativePath(ctx, originalRepo, defaultBundle)
	}

	writer := backup.NewLazyWriter(func() (io.WriteCloser, error) {
		return g.sink.getWriter(gCtx, bundlePath)
	})
	defer func() {
		if err := writer.Close(); err != nil && returnErr == nil {
			returnErr = fmt.Errorf("write bundle: %w", err)
		}
	}()

	opts := localrepo.CreateBundleOpts{
		Patterns: strings.NewReader(ref.String()),
	}

	timer := prometheus.NewTimer(g.bundleGenerationLatency)
	err = repo.CreateBundle(gCtx, writer, &opts)
	switch {
	case errors.Is(err, localrepo.ErrEmptyBundle):
		return structerr.NewFailedPrecondition("ref %q does not exist: %w", ref, err)
	case err != nil:
		g.logger.WithField("gl_project_path", repo.GetGlProjectPath()).
			WithError(err).
			Error("failed to generate bundle")
		return structerr.NewInternal("%w", err)
	}
	timer.ObserveDuration()
	g.bundleGenerationBytes.Add(float64(writer.BytesWritten()))
```

**File:** internal/bundleuri/strategy_occurences.go (L298-332)
```go
func (s *OccurrenceStrategy) evaluate(request evaluateRequest) bool {
	s.stateMu.Lock()
	defer s.stateMu.Unlock()

	state := s.loadState(request)

	// Here we are shifting all values in the slice
	// (state.occurrences) from one position to the end
	// in order to add the new value at the beginning.
	for i := s.threshold - 1; i > 0; i-- {
		state.occurrences[i] = state.occurrences[i-1]
	}
	state.occurrences[0] = request.time

	oldestOccurrence := state.occurrences[len(state.occurrences)-1]
	newestOccurrence := state.occurrences[0]

	// if the elapsed time between the newest and oldest occurrences
	// is not within the interval, we do not generate.
	if newestOccurrence.Sub(oldestOccurrence) > s.interval {
		return false
	}

	// if the last bundle generated is newer than the maxBundleAge, then
	// we do not generate again
	if state.lastGenerate.Add(s.maxBundleAge).After(request.time) {
		return false
	}

	if !state.generating {
		state.generating = true
		return true
	}
	return false
}
```

**File:** internal/bundleuri/strategy_occurences.go (L410-433)
```go
func (s *OccurrenceStrategy) startGenerationWorkers(ctx context.Context) {
	wg := sync.WaitGroup{}
	for i := 0; i < s.maxConcurrent; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-ctx.Done():
					return
				case request := <-s.generateQueue:
					err := request.cb(request.ctx, request.repo)
					if err != nil {
						s.logger.WithError(err).Error("failed to generate bundle")
						s.doneGenerating(request, false)
					} else {
						s.doneGenerating(request, true)
					}
				}
			}
		}()
	}
	wg.Wait()
}
```

**File:** internal/dontpanic/retry.go (L23-27)
```go
// Go will run the provided function in a goroutine and recover from any
// panics.  If a panic occurs, the recovered panic will be sent to Sentry
// and logged as an error. Go is best used in fire-and-forget goroutines where
// observability is lost.
func Go(logger log.Logger, fn func()) { go Try(logger, fn) }
```

**File:** internal/bundleuri/sink.go (L45-59)
```go
func (s *Sink) getWriter(ctx context.Context, relativePath string) (io.WriteCloser, error) {
	writer, err := s.bucket.NewWriter(ctx, relativePath, &blob.WriterOptions{
		// 'no-store' - we don't want the bundle to be cached as the content could be changed,
		// so we always want a fresh and up to date data
		// https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Cache-Control#cacheability
		// 'no-transform' - disallows intermediates to modify data
		// https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Cache-Control#other
		CacheControl: "no-store, no-transform",
		ContentType:  "application/octet-stream",
	})
	if err != nil {
		return nil, fmt.Errorf("new writer for %q: %w", relativePath, err)
	}
	return writer, nil
}
```
