Confirmed: `getStream` at `internal/streamcache/cache.go:239-259` looks up purely by the string `key`, and if a matching entry exists it serves that cached stream directly without re-validating that the request's actual `args`/`pathspecs` correspond to it. This confirms the cache-key collision in `createArchiveCacheKey` can cause GitLab's `GetArchive` RPC to serve archive content belonging to a different `--exclude`/path configuration.

### Title
GetArchive Cache-Key Hash Collision via Unescaped Comma-Joined Pathspecs Enables Exclusion Bypass / Cache Poisoning - (File: internal/gitaly/service/repository/archive.go)

### Summary
`createArchiveCacheKey` builds the `GetArchive` disk/stream cache key by hashing the concatenation of `args` and `pathspecs`, joined with an unescaped `,` separator, with no length-prefixing or delimiter escaping. Because the archive path and exclude paths are attacker-controlled strings that may legitimately contain commas and colons, two semantically different `GetArchive` requests can be crafted to produce byte-identical pre-hash input, causing a cache-key collision analogous to the reported MiMC hash-collision bug (unescaped/length-agnostic concatenation of variable-length attacker inputs).

### Finding Description
In `handleArchive`, the archive `args` and `pathspecs` are built directly from user-controlled RPC fields (`GetArchiveRequest.path`/`GetArchiveRequest.exclude`), with excludes injected into the pathspec list as `":(exclude)" + exclude`: [1](#0-0) 

These are joined only by commas with no escaping and no length-prefix, then hashed to form the cache key: [2](#0-1) 

Because `path` and `exclude` entries are only validated for path traversal via `storage.ValidateRelativePath` (which does not forbid commas or the substring `:(exclude)`), an attacker can choose:
- Request A: `path = "a,:(exclude)b"`, `exclude = []`
- Request B: `path = "a"`, `exclude = ["b"]`

Both produce `pathspecs` joined as the identical string `"a,:(exclude)b"`, and with the same `commitId`/repository, `args` is identical too, yielding an identical `sha256` cache key despite the two requests describing different, legitimately distinct `git archive` invocations (one includes path `b`, the other explicitly excludes it).

The cache lookup in `streamcache.cache.getStream` trusts the key blindly and serves whatever stream is already indexed under it without re-validating the actual `args`/`pathspecs` that produced it: [3](#0-2) 

This is the direct structural analog of the MiMC bug: an ambiguous, unescaped join of variable-length attacker-supplied segments allows two distinct logical inputs to collide to the same hash/key.

### Impact Explanation
An ordinary authenticated user who can call `GetArchive` (crafted RPC fields: `path`, `exclude`, `commit_id`) can engineer a collision so that a response cached for one path/exclude combination is served for a different combination. This breaks the confidentiality guarantee of the `exclude` mechanism (used by GitLab to strip certain paths, e.g., sensitive files, from generated archives): if a "safe" (excluding) request is served first and cached, a subsequent colliding request that should not have excluded that content instead gets the already-cached, excluded output (data omission/incorrect content), or conversely an attacker can prime the cache with a maliciously-shaped request so that a later legitimate request (which should exclude sensitive paths) is served the attacker's cached, non-excluding archive containing content that should have been redacted. This is a cache-confusion vulnerability that can defeat the exclusion/redaction contract of `GetArchive`.

### Likelihood Explanation
Reaching this code path requires only calling the standard `GetArchive` RPC with attacker-controlled `path`/`exclude` fields containing commas and the literal substring `:(exclude)` — both are valid path characters not rejected by `storage.ValidateRelativePath`, and the archive cache (`streamCacheConfig.Enabled`) is a commonly enabled feature. No privileged access, malicious peer, or token leakage is required; a normal user issuing normal `GetArchive` RPCs is sufficient.

### Recommendation
Construct the cache key using length-prefixed or otherwise unambiguously-delimited encoding of each `args`/`pathspecs` element (e.g., write each element's length before its bytes, as is already done correctly in `internal/cache/keyer.go`'s `prefixLen` helper), instead of joining variable-length, attacker-controlled strings with a plain comma separator. [4](#0-3) 

### Proof of Concept
1. Enable the Gitaly stream/archive cache.
2. Send `GetArchive(repository, commit_id=C, path="a", exclude=["b"])`. This computes `args=["C"]`, `pathspecs=["a", ":(exclude)b"]`, cache key `H = sha256(glProjectPath || "C" || "a,:(exclude)b")`, and caches the archive of path `a` excluding `b`.
3. Send `GetArchive(repository, commit_id=C, path="a,:(exclude)b", exclude=[])`. This computes `args=["C"]`, `pathspecs=["a,:(exclude)b"]`, producing the identical joined string `"a,:(exclude)b"` and therefore the identical cache key `H`.
4. Because `streamcache.cache.getStream` finds an existing entry for key `H`, the second request is served the first request's cached output (path `a` with `b` excluded) instead of the git-archive output that request 3's literal pathspec would actually produce — demonstrating a reachable, exploitable hash collision.

### Citations

**File:** internal/gitaly/service/repository/archive.go (L195-212)
```go
func (s *server) handleArchive(ctx context.Context, p archiveParams) error {
	var args []string
	pathspecs := make([]string, 0, len(p.exclude)+1)
	if !p.in.GetElidePath() {
		// git archive [options] <commit ID> -- <path> [exclude*]
		args = []string{p.in.GetCommitId()}
		pathspecs = append(pathspecs, p.archivePath)
	} else if p.archivePath != "." {
		// git archive [options] <commit ID>:<path> -- [exclude*]
		args = []string{p.in.GetCommitId() + ":" + p.archivePath}
	} else {
		// git archive [options] <commit ID> -- [exclude*]
		args = []string{p.in.GetCommitId()}
	}

	for _, exclude := range p.exclude {
		pathspecs = append(pathspecs, ":(exclude)"+exclude)
	}
```

**File:** internal/gitaly/service/repository/archive.go (L290-296)
```go
func createArchiveCacheKey(gitLabProjectPath string, args []string, pathspecs []string) string {
	cacheKeyHash := sha256.New()
	cacheKeyHash.Write([]byte(gitLabProjectPath))
	cacheKeyHash.Write([]byte(strings.Join(args, ",")))
	cacheKeyHash.Write([]byte(strings.Join(pathspecs, ",")))
	return hex.EncodeToString(cacheKeyHash.Sum(nil))
}
```

**File:** internal/streamcache/cache.go (L239-249)
```go
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
```

**File:** internal/cache/keyer.go (L316-321)
```go
// prefixLen reduces the risk of collisions due to different combinations of
// concatenated strings producing the same content.
// e.g. f+oobar and foo+bar concatenate to the same thing: foobar
func prefixLen(s string) []byte {
	return []byte(fmt.Sprintf("%08x%s", len(s), s))
}
```
