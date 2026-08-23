### Title
Cache key collision in `GetArchive` due to unbounded string concatenation - ([File: internal/gitaly/service/repository/archive.go])

### Summary
`createArchiveCacheKey` builds the `GetArchive` cache key by concatenating the GitLab project path, the `git archive` positional args, and the pathspecs using `strings.Join(..., ",")` with no length-prefixing or field-boundary delimiters. Because pathspecs are user-controlled strings that can themselves contain commas and the literal `":(exclude)"` magic marker, two semantically different `GetArchiveRequest`s can serialize to the identical byte stream fed into the SHA-256 hash, producing the same cache key. This is the same "ambiguous concatenation" root cause described in the reference report (untyped digests can collide when they aren't injective over the input space).

### Finding Description
`GetArchive` computes the cache key like this: [1](#0-0) 

`args` holds the commit ID (or `commitID:path` for `ElidePath`), and `pathspecs` holds the archive path plus `":(exclude)"+exclude` entries built from user-supplied `Path`/`Exclude` fields: [2](#0-1) 

The only bound on `Path`/`Exclude` is `storage.ValidateRelativePath`, which merely rejects path-traversal outside the repository root — it does not restrict the character set (commas, colons, and the `:(exclude)` sequence are all valid path characters): [3](#0-2) 

Because `strings.Join(pathspecs, ",")` uses `,` purely as a separator with no escaping or length-prefixing, a single pathspec entry containing an embedded `,` and the literal text `:(exclude)` can reproduce the exact same joined byte string as a request with a distinct `Path` plus a distinct `Exclude` entry, e.g.:
- Request A: `Path = "a,:(exclude)x"`, `Exclude = []` → pathspecs `["a,:(exclude)x"]` → joined `"a,:(exclude)x"`
- Request B: `Path = "a"`, `Exclude = ["x"]` → pathspecs `["a", ":(exclude)x"]` → joined `"a,:(exclude)x"`

With the same `commitID` (and therefore the same `args`) and same repository (`gitLabProjectPath`), both requests hash to the identical cache key even though they ask `git archive` to produce different content (one archives the literal path `a,:(exclude)x`; the other archives `a` while excluding `x`).

### Impact Explanation
The cache key feeds `s.archiveCache.Fetch`, a process-wide `streamcache.Cache` keyed purely by this hash string: [4](#0-3) [5](#0-4) 

If two different `GetArchive` calls collide on the cache key, whichever request populates the cache first determines the archive bytes returned to the second caller: an ordinary requester whose crafted `Path`/`Exclude` collides with another user's (or the same user's earlier) request can be served archive content for a different set of included/excluded paths than they asked for. This is a content-confusion/cache-poisoning bug: a caller who is authorized to fetch archives from the repository can obtain, via cache reuse, files that a properly-scoped exclude would have omitted (or vice versa), because the cache silently treats the two logically distinct requests as identical.

### Likelihood Explanation
Reaching this requires only an ordinary, authenticated `GetArchive` RPC caller (no privileged actor, no malicious peer, no leaked token) supplying a `Path`/`Exclude` value that exists in the tree and embeds `,` and `:(exclude)`. Both fields pass through only `ValidateRelativePath` (traversal check) and a tree-entry existence check, neither of which restricts these characters, so crafting the colliding pair is feasible for any client of the RPC. The colliding request must also race/contend for the cache with a legitimate request targeting the same commit/repo, which somewhat limits real-world exploitation but is squarely within the reachable RPC-handler resource/cache path called out in scope.

### Recommendation
Make the cache-key construction injective: length-prefix each field (as already done correctly in `internal/cache/keyer.go`'s `prefixLen` helper) instead of joining with a plain `,`, and hash each component (`gitLabProjectPath`, each `args` element, each `pathspecs` element) with an explicit length or a non-ambiguous delimiter that cannot occur inside the fields, e.g.:
```go
for _, part := range append([]string{gitLabProjectPath}, append(args, pathspecs...)...) {
    h.Write(prefixLen(part))
}
```
This mirrors the report's short-term fix of including a length/type prefix for every field before hashing so that no two distinct inputs can produce the same digest.

### Proof of Concept
1. In a repository, ensure a tree entry named literally `a,:(exclude)x` exists (a file or directory whose name contains a comma and the string `:(exclude)x`), alongside an entry `a` containing an entry `x`.
2. Call `GetArchive` with `CommitId = C`, `Path = "a,:(exclude)x"`, `Exclude = []`. This populates the cache under key `H = createArchiveCacheKey(proj, [C], ["a,:(exclude)x"])`.
3. Call `GetArchive` with `CommitId = C`, `Path = "a"`, `Exclude = ["x"]`. Its cache key is `createArchiveCacheKey(proj, [C], ["a", ":(exclude)x"])`, which hashes to the same `H` because `strings.Join` produces the identical string `"a,:(exclude)x"` in both cases.
4. The second call receives the archive produced by the first call's `git archive` invocation (archiving `a,:(exclude)x` verbatim) instead of an archive of `a` excluding `x`, demonstrating the cache-key collision and cross-request content confusion.

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

**File:** internal/gitaly/service/repository/archive.go (L242-252)
```go
	repo := s.localRepoFactory.Build(p.in.GetRepository())

	cacheKey := createArchiveCacheKey(repo.GetGlProjectPath(), args, pathspecs)
	_, _, err := s.archiveCache.Fetch(ctx, cacheKey, p.writer, func(writer io.Writer) error {
		archiveCommand, err := repo.Exec(ctx, gitcmd.Command{
			Name:        "archive",
			Flags:       []gitcmd.Option{gitcmd.ValueFlag{Name: "--format", Value: p.format}, gitcmd.ValueFlag{Name: "--prefix", Value: p.in.GetPrefix() + "/"}},
			Args:        args,
			PostSepArgs: pathspecs,
		}, gitcmd.WithEnv(env...), gitcmd.WithConfig(gitConfig...), gitcmd.WithSetupStdout())
		if err != nil {
```

**File:** internal/gitaly/service/repository/archive.go (L287-296)
```go
// createArchiveCacheKey creates a cache key using the GitLab project's path, the `git archive`
// command arguments and the pathspecs. The goal is to create a key that is unique not only
// across repository, but also across the content of each archive within the same repository.
func createArchiveCacheKey(gitLabProjectPath string, args []string, pathspecs []string) string {
	cacheKeyHash := sha256.New()
	cacheKeyHash.Write([]byte(gitLabProjectPath))
	cacheKeyHash.Write([]byte(strings.Join(args, ",")))
	cacheKeyHash.Write([]byte(strings.Join(pathspecs, ",")))
	return hex.EncodeToString(cacheKeyHash.Sum(nil))
}
```

**File:** internal/gitaly/storage/locator.go (L157-164)
```go
func ValidateRelativePath(rootDir, relativePath string) (string, error) {
	absPath := filepath.Join(rootDir, relativePath)
	if rootDir != absPath && !strings.HasPrefix(absPath, rootDir+string(os.PathSeparator)) {
		return "", ErrRelativePathEscapesRoot
	}

	return filepath.Rel(rootDir, absPath)
}
```

**File:** internal/streamcache/cache.go (L215-233)
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
```
