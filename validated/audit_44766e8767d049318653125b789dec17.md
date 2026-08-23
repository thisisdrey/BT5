## Analysis: Unsynchronized concurrent access to `mvcc.LocalCache` fields

The Celo report describes a data race caused by reading a mutable field from multiple goroutines without holding the mutex that guards its writer. The closest reachable analog in this Gitaly codebase is in the MVCC reference-backend cache, `internal/git/mvcc/cache.go`.

### Root cause

`LocalCache.PrepareWithManifest` kicks off `prepare()` in a background goroutine and returns immediately: [1](#0-0) 

Inside `prepare()`, the goroutine mutates several `LocalCache` fields **without any mutex**: the `m.environments` map, `m.baseHash`, and `m.baseManifest`: [2](#0-1) 

These same fields are read from `Commit()`, `ManifestPath()`, `ManifestHash()`, and `Environment()` — none of which acquire a lock or call `WaitUntilReady()` before touching them: [3](#0-2) [4](#0-3) 

The `LocalCache` struct has no `sync.Mutex` guarding `environments`, `baseHash`, `baseManifest`, or `committed` — only `sync.Once` guards are used to prevent the *goroutine* from being started twice: [5](#0-4) 

### Reachability

The MVCC interceptor is what wires this into every RPC that targets a repository configured with the MVCC reference backend. It starts the cache asynchronously via `cache.Prepare(ctx)`, invokes the RPC `handler`, and then immediately calls `cache.Commit(ctx)` once the handler returns — with **no call to `WaitUntilReady()` at the interceptor level**: [6](#0-5) 

The design relies on the handler itself (via Git command construction, presumably in `gitcmd`/command factory) calling `WaitUntilReady()` before it needs `Environment()`. But `Commit()` — called directly by the interceptor right after the handler returns — never synchronizes with the `prepare()` goroutine at all. If the handler's git-command path finishes (or never needs to build a git command that consults `Environment()`) before the background `prepare()` goroutine finishes writing `m.environments`/`m.baseHash`/`m.baseManifest`, `Commit()` runs concurrently with `prepare()`'s writes to the same map/fields.

### Title
Unsynchronized concurrent read/write of `LocalCache` fields in the MVCC reference-backend cache - (File: internal/git/mvcc/cache.go)

### Summary
`mvcc.LocalCache` starts an unsynchronized background goroutine (`prepare()`) that writes to the `environments` map and the `baseHash`/`baseManifest` fields. `Commit()`, `Environment()`, `ManifestHash()`, and `ManifestPath()` read these same fields from a different goroutine (the RPC/interceptor goroutine) without ever acquiring a mutex or waiting on `WaitUntilReady()`.

### Finding Description
`PrepareWithManifest` fires the cache-warming work in a detached goroutine (`internal/git/mvcc/cache.go:167-174`). That goroutine's `prepare()` function unconditionally writes to `m.environments[...]`, `m.baseHash`, and `m.baseManifest` (`internal/git/mvcc/cache.go:296-317`) with no lock. The `mvcc.CacheInterceptor` middleware calls `cache.Prepare(ctx)` and then, once `handler(ctx, req)` returns, immediately calls `cache.Commit(ctx)` (`internal/grpc/middleware/mvcc/mvcc.go:67-77`) — with no synchronization call in between at the interceptor layer. `Commit()` reads `m.ManifestPath()` (which indexes into the `environments` map) and `m.baseHash`/`m.baseManifest` without taking any lock (`internal/git/mvcc/cache.go:202-234`). Because `environments` is a plain Go `map[string]string`, an unsynchronized concurrent read (in `Commit`/`Environment`/`ManifestHash`/`ManifestPath`) racing with a concurrent write (in `prepare`) is a genuine Go data race.

### Impact Explanation
Go maps are not safe for concurrent read/write access; a race between the writer in `prepare()` and a reader in `Commit()`/`Environment()` can trigger the Go runtime's built-in concurrent-map-access detector, producing `fatal error: concurrent map read and map write`, which is unrecoverable and crashes the entire Gitaly process — a denial of service affecting every client connected to that node, not just the triggering request. Even absent a crash, `Commit()` may read a stale/zero-value `baseHash`/`baseManifest`, corrupting the MVCC compare-and-swap and manifest-diff logic used to publish new artifacts.

### Likelihood Explanation
The MVCC reference backend is feature-flagged and only used for repositories using that backend, so likelihood depends on how uniformly the RPC handler path enforces calling `WaitUntilReady()` before `Commit()` runs. Because that synchronization is not enforced in the `Cache` interface or the interceptor itself, any mutator RPC path that returns from `handler()` without having gone through a code path that calls `WaitUntilReady()` (e.g., a fast-completing mutator operation, or one whose git-command construction races with the still-running `prepare()` goroutine) exposes the race on an ordinary user's mutator request against an MVCC-backed repository.

### Recommendation
Guard `environments`, `baseHash`, `baseManifest`, and `committed` in `LocalCache` with a `sync.Mutex` (or equivalent), and have `Commit()`/`Environment()`/`ManifestHash()`/`ManifestPath()` explicitly call `WaitUntilReady()` (or otherwise block on `readyDone`) before accessing any of these fields, rather than relying on incidental ordering created by unrelated call sites.

### Proof of Concept
1. Enable the MVCC reference backend feature flag for a repository.
2. Issue a mutator RPC through `CacheInterceptor.UnaryInterceptor` such that the RPC handler completes (returns success) without invoking any Git subprocess that triggers `WaitUntilReady()`/`Environment()` internally, while `Prepare()`'s background `prepare()` goroutine is still executing (e.g., under load with a slow manifest/object-storage fetch).
3. The interceptor calls `cache.Commit(ctx)` immediately after the handler returns, which invokes `m.ManifestPath()` (reading the `environments` map) and reads `m.baseHash` concurrently with `prepare()`'s writes to the same fields, at `internal/git/mvcc/cache.go:296-317` vs. `internal/git/mvcc/cache.go:202-234`, producing a data race and potential process crash (`go test -race` would flag this).

### Citations

**File:** internal/git/mvcc/cache.go (L107-137)
```go
	// prepareDone is used to synchronize between the prepare phase and the ready phase.
	// This channel gets closed one cache warming is done.
	prepareDone chan struct{}

	// readyCh is closed once the preparation phase is done. It allows to wait for the
	// prepare phase to finish, so that we can capture the error into prepareErr.
	readyDone chan struct{}

	// prepareErr is the error returned by `prepare()`. We cache this value so that
	// subsequent call to `WaitForReady` get the same value everytime. This insures
	// consistent behavior when called multiple time.
	prepareErr error

	// prepareOnce makes sure the cache is only warmed once.
	prepareOnce sync.Once

	// readyOnce insures that, if `prepare` returns an error, multiple calls to
	// WaitForReady returns that same error. Unless of course the context is canceled.
	// In that case it returns the context error.
	readyOnce sync.Once

	// baseHash is the manifest pointer resolved at the start of a mutator RPC. It is
	// the base against which the commit-time compare-and-swap is performed.
	baseHash string
	// baseManifest is the parsed manifest referenced by baseHash. It is used to
	// compute which artifacts a mutator RPC newly produced.
	baseManifest *Manifest
	// committed guards Commit so the canonical manifest pointer is advanced at most
	// once per RPC.
	committed bool
}
```

**File:** internal/git/mvcc/cache.go (L167-174)
```go
func (m *LocalCache) PrepareWithManifest(ctx context.Context, hash string) {
	m.prepareOnce.Do(func() {
		go func() {
			m.prepareErr = m.prepare(ctx, hash)
			close(m.prepareDone)
		}()
	})
}
```

**File:** internal/git/mvcc/cache.go (L202-234)
```go
func (m *LocalCache) Commit(ctx context.Context) error {
	if m.readOnly {
		return nil
	}

	if m.ManifestPath() == "" {
		return fmt.Errorf("empty GIT_MVCC_MANIFEST_PATH")
	}

	// Assert the at-most-once property: advancing the canonical pointer more than
	// once for a single RPC would result in a torn write.
	if m.committed {
		return fmt.Errorf("mvcc: commit called more than once")
	}
	m.committed = true

	newHashBytes, err := os.ReadFile(m.ManifestPath())
	if err != nil {
		return fmt.Errorf("read from manifest path: %w", err)
	}
	newHash := strings.TrimSuffix(string(newHashBytes), "\n")
	if newHash == m.baseHash {
		return nil // no changes, nothing to publish
	}

	// Upload the new artifacts before advancing the pointer so the pointer never
	// references an artifact that is not yet durable.
	if err := m.putNewArtifacts(ctx, newHash); err != nil {
		return fmt.Errorf("put new artifacts: %w", err)
	}

	// Atomically advance the canonical manifest pointer.
	return m.rs.Commit(ctx, m.repositoryID, m.baseHash, newHash)
```

**File:** internal/git/mvcc/cache.go (L244-268)
```go
// Environment returns the list of environment variables that must be passed to
// each Git commands executed on the repository when using this MVCC cache.
func (m *LocalCache) Environment() []EnvVar {
	var result []EnvVar
	for k, v := range m.environments {
		result = append(result, EnvVar{Name: k, Value: v})
	}
	return result
}

// ManifestHash is a helper function to retrieve the manifest hash
// used for a read-only RPC. It returns an empty string when not set
// or when the RPC is a mutator one. This function is not part of the
// Cache interface, and is mostly used for testing.
func (m *LocalCache) ManifestHash() string {
	return m.environments["GIT_MVCC_MANIFEST"]
}

// ManifestPath is a helper function to retrieve the manifest path
// used for a mutator RPC. It returns an empty string when not set
// or when the RPC is an accessor one. This function is not part of the
// Cache interface, and is mostly used for testing.
func (m *LocalCache) ManifestPath() string {
	return m.environments["GIT_MVCC_MANIFEST_PATH"]
}
```

**File:** internal/git/mvcc/cache.go (L296-317)
```go
	if m.readOnly {
		m.environments["GIT_MVCC_MANIFEST"] = hash
	} else {
		// For mutator RPCs, we create a temp manifest pointer file that points to the manifest file
		tmpManifestFileDir := filepath.Join(m.runtimeDir, mvccCacheDir, m.repositoryID)
		err = os.MkdirAll(tmpManifestFileDir, 0o755)
		if err != nil {
			return fmt.Errorf("make manifest temp dir: %w", err)
		}

		tmpManifestFile, err := os.CreateTemp(tmpManifestFileDir, "manifest-*")
		if err != nil {
			return fmt.Errorf("create temp manifest file: %w", err)
		}
		defer func() { _ = tmpManifestFile.Close() }()
		if _, err := tmpManifestFile.WriteString(hash); err != nil {
			return fmt.Errorf("write temp manifest file: %w", err)
		}
		m.baseHash = hash
		m.baseManifest = manifest
		m.environments["GIT_MVCC_MANIFEST_PATH"] = tmpManifestFile.Name()
	}
```

**File:** internal/grpc/middleware/mvcc/mvcc.go (L62-80)
```go
		cache, err := m.buildCache(ctx, targetRepo, methodInfo.Operation)
		if err != nil || cache == nil {
			return handler(ctx, req)
		}

		cache.Prepare(ctx)
		defer cache.Cleanup()
		ctx = mvcc.ContextWithCache(ctx, cache)
		ctx = gitcmd.ContextWithReferenceBackend(ctx, git.ReferenceBackendMVCC)
		res, err := handler(ctx, req)
		if err != nil {
			return nil, err
		}
		if err := cache.Commit(ctx); err != nil {
			return nil, mapCommitError(err)
		}

		return res, nil
	}
```
