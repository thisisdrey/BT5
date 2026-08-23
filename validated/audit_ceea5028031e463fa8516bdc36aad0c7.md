### Title
Ambiguous concatenation in `GetArchive` cache-key construction lets crafted requests collide and poison the shared archive cache - ([File: internal/gitaly/service/repository/archive.go])

### Summary
`createArchiveCacheKey` builds the `RepositoryService.GetArchive` cache key by writing `gitLabProjectPath`, `strings.Join(args, ",")` and `strings.Join(pathspecs, ",")` into a single SHA-256 hash with no delimiters between the three segments [1](#0-0) . Because the `args`/`pathspecs` boundary is fully attacker-controlled per request (via `commit_id`, `path`, and `exclude`), two semantically different `GetArchiveRequest`s can be crafted whose concatenated byte streams are identical, producing the same cache key and colliding in the shared, server-wide `streamcache.Cache` used by `archiveCache.Fetch`.

### Finding Description
`handleArchive` builds `args` from `commit_id` (optionally combined with the elided path) and `pathspecs` from the archive path and each `exclude` entry (prefixed with `:(exclude)`) [2](#0-1) . These are then fed straight into `createArchiveCacheKey(repo.GetGlProjectPath(), args, pathspecs)` [3](#0-2) , which simply concatenates the joined strings without a length prefix or separator between the `args` block and the `pathspecs` block [1](#0-0) .

`git.ValidateRevision`, which validates `commit_id`, forbids `:`, whitespace, `\`, and NUL, but does **not** forbid commas [4](#0-3) . The archive `path`/`exclude` values are only checked with `storage.ValidateRelativePath` for path traversal, which likewise does not exclude commas (valid Git tree entries may contain commas). This lets a caller shift a comma across the `args`/`pathspecs` boundary while keeping the raw byte sequence written to the hash identical, e.g.:

- Request A: `commit_id="abcd"`, `path="ef"` → `args=["abcd"]`, `pathspecs=["ef"]` → written bytes `...` + `"abcd"` + `"ef"`
- Request B: `commit_id="abc"`, `path="def"` → `args=["abc"]`, `pathspecs=["def"]` → written bytes `...` + `"abc"` + `"def"`

Both produce the identical concatenated byte stream `"abcdef"` (given the same `gitLabProjectPath` prefix, i.e. same repository), and therefore the identical SHA-256 cache key, even though the two requests ask for archives of different commits/paths.

The resulting key is used with the shared `streamcache.Cache.Fetch` [5](#0-4) , a server-wide cache (not scoped per-session), so a collision causes the second, colliding request to be served the first request's already-cached archive content instead of computing/serving its own.

### Impact Explanation
An unprivileged user with read access to a repository who can issue `GetArchive` RPCs can:
1. Prime the cache with an archive of a commit/path of their choosing.
2. Cause a subsequent, unrelated `GetArchive` request for a different commit/path/exclude combination that happens to collide (by design or by chance, since the collision space is easy to construct) to receive the wrong, previously-cached archive content instead of the content matching its own parameters.

This breaks the correctness guarantee of the archive cache and can leak content from one commit/path to a caller who requested a different commit/path within the same repository, or cause persistently incorrect archive responses until the cache entry expires — a data-integrity/confidentiality violation of the RPC handler analogous to the referenced report's transaction-hash collision that silently substitutes one entity's state for another's.

### Likelihood Explanation
Reachable via a single, standard, unprivileged `RepositoryService.GetArchive` RPC call using only request-controlled fields (`commit_id`, `path`, `exclude`); no elevated privileges, malicious peer, or MITM assumptions are required. Constructing a colliding pair of requests is a simple string-length shift, well within the validation constraints of `git.ValidateRevision`/`storage.ValidateRelativePath`.

### Recommendation
Include explicit, unambiguous length-prefixed or delimiter-safe encoding of each field when constructing the cache key — e.g., write the length of each segment before its bytes (as done in the disk-cache's `compositeKeyHashHex`/`prefixLen` helper elsewhere in the codebase) [6](#0-5)  instead of `strings.Join` and raw concatenation, so that no two distinct `(project path, args, pathspecs)` tuples can ever hash to the same key.

### Proof of Concept
1. On a repository, issue `GetArchive` with `commit_id="abcd"`, `path="ef"`, no excludes — this populates `archiveCache` under key `SHA256(gitLabProjectPath + "abcd" + "ef")`.
2. Issue a second `GetArchive` with `commit_id="abc"`, `path="def"`, no excludes, on the same repository — this computes the same key `SHA256(gitLabProjectPath + "abc" + "def")` = `SHA256(gitLabProjectPath + "abcdef")`, identical to step 1's key.
3. The second request hits the cache populated in step 1 and receives the archive for commit `abcd` path `ef` instead of commit `abc` path `def`, confirmed by comparing the returned archive contents against the actual tree at `abc:def`.

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

**File:** internal/gitaly/service/repository/archive.go (L242-244)
```go
	repo := s.localRepoFactory.Build(p.in.GetRepository())

	cacheKey := createArchiveCacheKey(repo.GetGlProjectPath(), args, pathspecs)
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

**File:** internal/git/revision.go (L86-96)
```go
	}
	if bytes.ContainsAny(revision, " \t\n\r") {
		return fmt.Errorf("revision can't contain whitespace")
	}
	if bytes.Contains(revision, []byte(":")) {
		return fmt.Errorf("revision can't contain ':'")
	}
	if bytes.Contains(revision, []byte("\\")) {
		return fmt.Errorf("revision can't contain '\\'")
	}

```

**File:** internal/streamcache/cache.go (L42-51)
```go
type Cache interface {
	// Fetch finds or creates a cache entry and writes its contents into dst.
	// If the create callback is called the created return value is true. In
	// case of a non-nil error return, the create callback may still be
	// running in a goroutine for the benefit of another caller of Fetch with
	// the same key.
	Fetch(ctx context.Context, key string, dst io.Writer, create func(io.Writer) error) (written int64, created bool, err error)
	// Stop stops the cleanup goroutines of the cache.
	Stop()
}
```

**File:** internal/cache/keyer.go (L300-311)
```go
	for _, i := range []string{
		version.GetVersion(),
		method,
		genID,
		string(reqSum),
		strings.Join(flagsWithValue, " "),
	} {
		_, err := h.Write(prefixLen(i))
		if err != nil {
			return "", err
		}
	}
```
