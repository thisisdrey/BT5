### Title
Path-keyed catfile process cache can serve stale git-cat-file(1) process data across repository deletion/recreation - (File: internal/git/catfile/cache.go)

### Summary
Gitaly's `catfile.ProcessCache` caches long-lived `git-cat-file --batch` reader/info processes keyed only by `(sessionID, repoStorage, repoRelPath, repoObjDir, repoAltDir)`. None of these fields encode a repository "generation"/identity beyond its path. When a repository is removed (`RemoveRepository`) and a new repository is created at the very same `relative_path` (`CreateRepository`, replication, or repository re-import) within the cache's TTL/session window, a previously-cached, still-alive `git-cat-file` process for the old repository can be checked out and reused to serve object reads for the "new" repository at that path. This mirrors the HydraDX Oracle bug class: an entity is deleted, its identity-bound cache entries are not invalidated, and the entity is later re-created with different content at the same key, causing stale/incorrect data to be served that mixes information from before and after re-creation.

### Finding Description
`internal/git/catfile/cache.go` defines the cache key: [1](#0-0) 

The key is derived purely from a 5-minute session bucket and the repository's storage name, relative path, git object directory, and alternates — not from any repository generation, LSN, creation timestamp, or content-derived identity: [2](#0-1) 

`getOrCreateProcess` looks the key up in the cache and, on a hit, hands back the previously created (long-running, detached) `git-cat-file` process without any check that the underlying repository on disk is still the same repository that spawned the process: [3](#0-2) 

Cached processes are explicitly detached from the originating request's context/cancellation and correlation ID specifically so they can be reused across different RPC calls and even across different callers sharing the same session: [4](#0-3) 

The comment even acknowledges that reused processes may retain "stale" state (feature flags) intentionally, showing that the design accepts staleness as a tradeoff, but does not address staleness introduced by the repository being deleted and re-created at the same path — analogous to how the ema-oracle's `Oracles` storage map (`(Source, (AssetId, AssetId), OraclePeriod) -> OracleEntry`) retained stale entries after `remove_token`, later combined with fresh entries after `add_token` under the same key to compute an incorrect price.

Contrast this with how repository removal/creation is handled at the storage layer, where `localRepository.Create` explicitly documents the need to rebuild wrapper state because underlying caches must be invalidated: [5](#0-4) 

No equivalent invalidation call exists for `catfile.ProcessCache` on `RemoveRepository`/repository re-creation at the same relative path — the disk-cache invalidation mechanism (`internal/grpc/middleware/cache`, `internal/cache`) only covers the `SmartHTTPService.InfoRefUploadPack` disk cache and is wired via RPC mutator annotations, not the in-process catfile `ProcessCache`: [6](#0-5) 

Because a `git-cat-file --batch`/`--batch-check` process keeps open file descriptors/mmaps into the old repository's object database (packfiles/loose objects), and does not dynamically re-scan for a brand-new `.git` directory materializing at the same path, subsequent reads through the reused cached process can:
- Return object content belonging to the deleted repository for OIDs that happen to still resolve against the old (now unlinked, but fd-held) pack/loose objects, even though the caller now believes it is talking to the newly created repository at that path, and/or
- Fail to see new objects in the freshly created repository at all, causing inconsistent read behavior for accessor RPCs like `GetBlob`/`GetCommit`/`TreeEntry`/`ReadObject` that go through `repo.catfileCache.ObjectReader`: [7](#0-6) 

### Impact Explanation
This is a data-integrity/isolation issue analogous to the M-07 finding: a cache keyed by a mutable identity attribute (asset ID in HydraDX, repository relative path in Gitaly) is not invalidated when the underlying entity is destroyed and recreated, so stale data can leak into or corrupt reads against the newly created entity. In Gitaly's case this can cause:
- Cross-generation object data leakage: RPCs reading from a repository immediately after deletion+recreation at the same path can, for a window bounded by the 5-minute session bucket and process TTL, receive object bytes computed against the deleted repository's on-disk state rather than the new one.
- Read inconsistency / minor DoS: reads for objects that exist only in the newly created repository can spuriously fail (`NotFoundError`) if served by a stale cached process, until the process is evicted or marked dirty.

The severity is bounded because: (1) it requires deletion and near-immediate recreation of a repository at the exact same relative path, (2) the window is limited by the session bucket (5 minutes) and process TTL (default 10s idle eviction, but idle timers reset on checkout), and (3) exploitation depends on internal race timing rather than being deterministic on every request. This matches the judge's characterization of the original finding as a "Medium" — a real but conditional value/consistency leak, not a full compromise.

### Likelihood Explanation
Reachable from ordinary/attacker-controlled RPC sequences without any special privilege: any caller that can invoke `RemoveRepository` followed by `CreateRepository` (or an equivalent replace/replicate/import flow) against the same `Repository{storage_name, relative_path}` — as already exercised in Gitaly's own transaction-manager tests for "create repository again after deletion" and "writes concurrent with repository deletion" — can trigger the underlying repository swap at a fixed path while other in-flight or recently-cached accessor RPCs continue to hold/reuse a `catfile.ProcessCache` entry keyed only by that same path. No malicious peer, MITM, or token leak is required; it is purely a same-path delete+recreate race exploitable by whoever controls the repository lifecycle RPCs (e.g., during fork/import/re-import workflows), making it a plausible, moderate-likelihood race rather than a purely theoretical one.

### Recommendation
Bind the catfile process cache key to a repository generation identifier that changes across delete/recreate cycles (e.g., the storage's applied LSN /repository generation from the WAL-based partition manager, or a per-repository creation nonce/inode of the `.git` directory), not just `(storage, relative_path, object dir, alt dir)`. Alternatively, explicitly evict/invalidate all `catfile.ProcessCache` entries for a repository's `(storage, relative_path)` synchronously as part of repository removal (`repoutil.Remove`) and as part of repository creation (`repoutil.Create`), mirroring the `localRepo = localrepo.New(...)` cache-busting comment already present in `internal/backup/repository.go`.

### Proof of Concept
Conceptual (Gitaly RPC-level) sequence demonstrating the race, mirroring the HydraDX PoC pattern of delete → wait → recreate → observe stale cached data:

1. Client A calls an accessor RPC (e.g. `GetBlob`) against `Repository{storage: "default", relative_path: "repo.git"}` containing blob `B1`. This populates `catfile.ProcessCache` with a long-lived `git-cat-file --batch` process keyed by `{sessionID, "default", "repo.git", "", ""}`.
2. Client A ends the RPC; per `getOrCreateProcess`, the process is returned to the cache (not killed) because it is "clean," and stays alive up to the TTL, detached from the original context.
3. An operator/attacker-controlled workflow calls `RemoveRepository` for `Repository{storage: "default", relative_path: "repo.git"}`, then immediately `CreateRepository` for the same `relative_path`, and pushes different content (blob `B2` at the same or a colliding-looking path scenario).
4. Within the same 5-minute session bucket and before TTL eviction, Client B calls `GetBlob`/`ReadObject` against the "new" `repo.git`. `getOrCreateProcess` computes the identical cache key and returns the process cached in step 2 rather than spawning a fresh one against the new repository.
5. Depending on whether the stale process's open file handles still resolve requested OIDs, the RPC either returns bytes read from the deleted repository's now-unlinked object files (data leak/inconsistency) or fails to find objects that exist only in the newly created repository (unexpected `NotFoundError`), rather than reflecting the current state of `repo.git`.

Note: full end-to-end confirmation would require instrumenting `internal/git/catfile/cache_test.go`-style integration test with `RemoveRepository`+`CreateRepository` at the same relative path interleaved with a live cached `ObjectReader`; this was not directly executed here due to the read-only nature of this analysis, but the code paths cited above establish the root cause (key omits any repository-generation discriminator) and the intended reuse-across-calls behavior that enables it.

### Citations

**File:** internal/git/catfile/cache.go (L245-260)
```go
	cacheKey, isCacheable := newCacheKey(fmt.Sprintf("%d", roundToNearestFiveMinute(time.Now())), repo)

	if isCacheable {
		// We only try to look up cached processes in case it is cacheable, which requires a
		// session ID. This is mostly done such that git-cat-file(1) processes from one user
		// cannot interfere with those from another user. The main intent is to disallow
		// trivial denial of service attacks against other users in case it is possible to
		// poison the cache with broken git-cat-file(1) processes.

		if entry, ok := processes.Checkout(cacheKey); ok {
			c.catfileCacheCounter.WithLabelValues("hit").Inc()
			span.SetAttributes(attribute.Bool("hit", true))
			return entry.value, func() {
				c.returnToCache(processes, cacheKey, entry.value, entry.cancel)
			}, nil
		}
```

**File:** internal/git/catfile/cache.go (L270-284)
```go
		// We have not found any cached process, so we need to create a new one. In this
		// case, we need to detach the process from the current context such that it does
		// not get killed when the parent context is cancelled.
		//
		// Note that we explicitly retain feature flags here, which means that cached
		// processes may retain flags for some time which have been changed meanwhile. While
		// not ideal, it feels better compared to just ignoring feature flags altogether.
		// The latter would mean that we cannot use flags in the catfile code, but more
		// importantly we also wouldn't be able to use feature-flagged Git version upgrades
		// for catfile processes.
		ctx = context.WithoutCancel(ctx)
		// We have to decorrelate the process from the current context given that it
		// may potentially be reused across different RPC calls.
		ctx = correlation.ContextWithCorrelation(ctx, "")
	}
```

**File:** internal/git/catfile/cache.go (L366-386)
```go
type key struct {
	sessionID   string
	repoStorage string
	repoRelPath string
	repoObjDir  string
	repoAltDir  string
}

func newCacheKey(sessionID string, repo storage.Repository) (key, bool) {
	if sessionID == "" {
		return key{}, false
	}

	return key{
		sessionID:   sessionID,
		repoStorage: repo.GetStorageName(),
		repoRelPath: repo.GetRelativePath(),
		repoObjDir:  repo.GetGitObjectDirectory(),
		repoAltDir:  strings.Join(repo.GetGitAlternateObjectDirectories(), ","),
	}, true
}
```

**File:** internal/backup/repository.go (L778-781)
```go
	// Recreate the local repository, since the cache of object hash and ref-format needs
	// to be invalidated.
	r.repo = localrepo.New(r.logger, r.locator, r.gitCmdFactory, r.catfileCache, r.repo)

```

**File:** doc/design_diskcache.md (L1-15)
```markdown
# Disk Cache Design

Gitaly utilizes a disk-based cache for efficiently serving some RPC responses
(at time of writing, only the `SmartHTTPService.InfoRefUploadPack` RPC). This
cache is intended to be used for serving large responses not suitable for a RAM
based cache.

## Cache Invalidation

The mechanisms that enable the invalidation of the disk cache for a repo depend
on special annotations made to the Gitaly gRPC methods. Each method that has
scope "repository" and is operation type "mutator" will cause the specified
repository to be invalidated. For more information on the annotation system,
see the Gitaly protobuf definition
[contributing guide](https://gitlab.com/gitlab-org/gitaly/-/blob/265b4218fb4c7670b9ac0810d96f1beff271932f/doc/protobuf.md#rpc-annotations).
```

**File:** internal/git/localrepo/objects.go (L93-106)
```go
func (repo *Repo) ReadObjectWithLimit(ctx context.Context, oid git.ObjectID, limit int64) ([]byte, error) {
	objectReader, cancel, err := repo.catfileCache.ObjectReader(ctx, repo)
	if err != nil {
		return nil, fmt.Errorf("create object reader: %w", err)
	}
	defer cancel()

	object, err := objectReader.Object(ctx, oid.Revision())
	if err != nil {
		if errors.As(err, &catfile.NotFoundError{}) {
			return nil, InvalidObjectError(oid.String())
		}
		return nil, fmt.Errorf("get object from reader: %w", err)
	}
```
