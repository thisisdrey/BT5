## Finding

### Title
Cache key isolation for git-cat-file processes is broken by a coarse, non-unique time bucket, allowing cross-request process sharing and DoS - ([File: internal/git/catfile/cache.go])

### Summary
The catfile `ProcessCache` is supposed to key cached `git-cat-file` processes by a per-request/session identifier so that unrelated requests never share a live process, but `getOrCreateProcess` derives that "session ID" purely from the wall-clock minute via `roundToNearestFiveMinute(time.Now())` instead of any caller/session-scoped value.

### Finding Description
`getOrCreateProcess` builds the cache key as: [1](#0-0) 

The comment directly above states the intent: session IDs exist "such that git-cat-file(1) processes from one user cannot interfere with those from another user" and to "disallow trivial denial of service attacks against other users in case it is possible to poison the cache with broken git-cat-file(1) processes." [2](#0-1) 

However, `roundToNearestFiveMinute` only looks at `t.Minute()` and ignores the hour/day entirely: [3](#0-2) 

This has two compounding effects:
1. Instead of a real per-user/per-session token, the "session ID" is a coarse, globally shared, entirely predictable value (one of only 12 possible values: 5,10,...,60), identical for every concurrent request server-wide within the same 5-minute-of-hour window, and it recurs every hour.
2. Because the repository-derived fields (`repoStorage`, `repoRelPath`, `repoObjDir`, `repoAltDir`) are the only other components of the cache key, any two unrelated requests hitting the same repository (and same object/alternate directories) within the same time bucket resolve to the identical cache key and can `Checkout` and reuse the very same underlying `git-cat-file` process: [4](#0-3) [5](#0-4) 

This is reachable by any ordinary user through normal RPCs that read objects (`ObjectReader`/`ObjectInfoReader`, used pervasively by commit/tree/blob-reading RPCs) against a shared/forked repository — no privileged access, leaked token, or malicious peer needed. A search for the expected per-request `gitaly-session-id` metadata mechanism shows it is only referenced in test code, not consumed by the actual cache-key derivation in `cache.go`, confirming the session isolation was effectively replaced by this time-bucket scheme.

### Impact Explanation
The cache's explicit safety goal — isolating `git-cat-file` processes per caller to prevent one user's requests from poisoning another's cached process — is defeated. Any user who can trigger reads against the same repository can, within the same coarse time window, cause their request to reuse (and potentially corrupt/kill) a `git-cat-file` process that a concurrent, unrelated request is also relying on. Because dirty/closed processes are discarded rather than served, the primary practical consequence is denial-of-service: an attacker can deliberately churn or break the shared process (e.g., by triggering errors, hanging reads, or forcing eviction/close) during the window, degrading or interrupting cat-file access for concurrent unrelated requests on the same repository, matching the DoS-of-handler impact class.

### Likelihood Explanation
High reachability: `ObjectReader`/`ObjectInfoReader` are invoked by ordinary read-path RPCs for any repository, requiring no special privileges. The 5-minute-of-hour bucket is small and entirely predictable (an attacker can simply issue requests and observe/target the current window), and the bug in `roundToNearestFiveMinute` (ignoring hour/day) further increases collision frequency across unrelated time periods.

### Recommendation
Derive the cache-key "session ID" from an actual caller-scoped identifier (e.g., the per-request `gitaly-session-id` gRPC metadata value used elsewhere in the codebase/tests) rather than from a coarse, globally-shared wall-clock bucket. If a time-based TTL bucket is still desired for expiry bookkeeping, it must be combined with, not substituted for, a real per-session identifier. Additionally fix `roundToNearestFiveMinute` to include the full timestamp (not just minute-of-hour) if it continues to be used at all.

### Proof of Concept
1. Two unrelated gRPC clients (different sessions/users) issue read RPCs (e.g., `TreeEntry`, `GetBlob`, `FindCommit`) against the same repository within the same 5-minute-of-hour window.
2. Both requests compute the identical cache key via `newCacheKey(fmt.Sprintf("%d", roundToNearestFiveMinute(time.Now())), repo)` since `roundToNearestFiveMinute` yields the same bucket for both and the repo-derived fields match.
3. The second request's `getOrCreateProcess` call finds and `Checkout`s the first request's live `git-cat-file` process via `processes.Checkout(cacheKey)` — demonstrating cross-session process sharing.
4. An attacker-controlled client can then intentionally cause the shared process to become dirty/broken (e.g., partial reads) to disrupt the concurrent unrelated request relying on the same cached process, producing errors/DoS for that other request. [6](#0-5)

### Citations

**File:** internal/git/catfile/cache.go (L227-231)
```go
// Calculate the nearest 5-minute interval. For our cache key, we want to
// enforce expiry after 5 minutes.
func roundToNearestFiveMinute(t time.Time) int {
	return ((t.Minute() / 5) + 1) * 5
}
```

**File:** internal/git/catfile/cache.go (L233-260)
```go
func (c *ProcessCache) getOrCreateProcess(
	ctx context.Context,
	repo storage.Repository,
	processes *processes,
	create func(context.Context) (cacheable, error),
	spanName string,
) (_ cacheable, _ func(), returnedErr error) {
	defer c.reportCacheMembers()

	span, ctx := tracing.StartSpanIfHasParent(ctx, spanName, nil)
	defer span.End()

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

**File:** internal/git/catfile/cache.go (L374-386)
```go
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
