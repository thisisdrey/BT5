### Title
Hash-collision in `GetArchive` cache key allows cross-repository archive content disclosure - (File: internal/gitaly/service/repository/archive.go)

### Summary
`createArchiveCacheKey` builds the key used for the server-wide, in-memory `archiveCache` (a `streamcache.Cache`) by concatenating raw, variable-length, attacker-influenced fields with `sha256.Write` calls without any length-prefixing or unambiguous delimiter between fields, analogous to the `abi.encodePacked` hash-collision bug class described in the report.

### Finding Description
`GetArchive` computes its cache key as: [1](#0-0) 

```go
func createArchiveCacheKey(gitLabProjectPath string, args []string, pathspecs []string) string {
	cacheKeyHash := sha256.New()
	cacheKeyHash.Write([]byte(gitLabProjectPath))
	cacheKeyHash.Write([]byte(strings.Join(args, ",")))
	cacheKeyHash.Write([]byte(strings.Join(pathspecs, ",")))
	return hex.EncodeToString(cacheKeyHash.Sum(nil))
}
```

This concatenates three variable-length, attacker-influenced strings (`gitLabProjectPath`, joined `args`, joined `pathspecs`) with no length prefix or delimiter between the three fields, and `strings.Join(..., ",")` itself is ambiguous whenever any array element can contain a comma. `args` is built from `p.in.GetCommitId()` (and optionally the archive path), and `pathspecs` is built from user-supplied `path`/`exclude` values (validated only as legal relative paths, which may still contain commas), so two semantically different `GetArchive` requests — potentially against **different repositories** with different `GlProjectPath` values — can be crafted to serialize to the identical concatenated byte string and thus collide on the SHA-256 hash: [2](#0-1) [3](#0-2) 

The resulting `cacheKey` is used as the lookup key in `s.archiveCache`, which is a single, node-wide `streamcache.Cache` instance shared by the whole `RepositoryService` (not partitioned per repository, per storage, or per caller): [4](#0-3) [5](#0-4) 

The `streamcache.Cache.Fetch` implementation looks entries up purely by this string key and streams back whatever bytes were stored for that key to any caller who supplies a matching key, regardless of which repository/request originally produced it: [6](#0-5) [7](#0-6) 

Note that `gitLabProjectPath` comes from `repo.GetGlProjectPath()`, i.e., a field on the `Repository` protobuf message that is populated by the RPC caller (Rails/Workhorse forwards it, but it is carried in the request itself and is not independently re-derived/verified against the actual on-disk repository by `GetArchive`), while `args`/`pathspecs` are derived directly from client-controlled `CommitId`, `Path`, and `Exclude` request fields (only validated for path traversal, not for comma-safety or length-prefixing): [8](#0-7) 

### Impact Explanation
Because the cache is global to the Gitaly node and keyed only by this collidable hash, an attacker who can trigger `GetArchive` requests (any user able to invoke the RPC, e.g., via a fork/clone/download-archive path for a repository they control) can:
1. Craft a request whose `(GlProjectPath, args, pathspecs)` triple concatenates to the same bytes as a legitimate, previously-cached request for a **different** repository.
2. Receive that other repository's cached archive content (`Fetch` streams cached bytes to any caller presenting the matching key) — a cross-repository confidentiality breach, or conversely poison the cache so subsequent legitimate requests for the victim repository/path receive the attacker's payload if their content is cached under the colliding key first.

This matches the "cross-repository object access" class explicitly called out as an acceptable analog.

### Likelihood Explanation
Exploitation requires: (a) triggering `GetArchive` for a repository the attacker can name a commit/path/exclude list on (self-service, e.g., their own project or a public repo), and (b) constructing byte sequences that collide with a target's project path / commit ID / archive path / exclude pathspecs. Since `GlProjectPath` values follow a predictable `namespace/project` convention, `strings.Join(args, ",")` and `strings.Join(pathspecs, ",")` are trivially confusable by shifting a comma-containing boundary between fields (e.g., moving characters from the project path into `args`, or from one pathspec into another via commas in an exclude path), the collision construction is straightforward — similar to the `LibMuon` PoC, this is largely a matter of picking equal-length concatenations. However, actually causing content disclosure additionally requires timing/coordination so the victim's response gets cached before or concurrently with the attacker's colliding fetch, which lowers real-world reliability somewhat.

### Recommendation
Use unambiguous, length-prefixed encoding for the cache key inputs (mirroring the `prefixLen`/`compositeKeyHashHex` pattern already used elsewhere in the codebase, see `internal/cache/keyer.go`), e.g.:
```go
func createArchiveCacheKey(gitLabProjectPath string, args []string, pathspecs []string) string {
	h := sha256.New()
	for _, part := range append([]string{gitLabProjectPath}, append(args, pathspecs...)...) {
		fmt.Fprintf(h, "%08x%s", len(part), part)
	}
	return hex.EncodeToString(h.Sum(nil))
}
```
Additionally include the repository's `StorageName`/`RelativePath` (not just `GlProjectPath`) as a length-prefixed component to strongly bind cache entries to the exact repository, closing off any residual cross-repository leakage even if a hash collision is otherwise achieved.

### Proof of Concept
Given `createArchiveCacheKey(gitLabProjectPath, args, pathspecs)`, note that `sha256.Write` is called sequentially for `gitLabProjectPath`, `strings.Join(args, ",")`, and `strings.Join(pathspecs, ",")` with no separators between these three writes, and `strings.Join` inserts plain commas between slice elements. Two requests satisfying:

```
gitLabProjectPath_A + strings.Join(args_A, ",") + strings.Join(pathspecs_A, ",")
    ==
gitLabProjectPath_B + strings.Join(args_B, ",") + strings.Join(pathspecs_B, ",")
```//byte-for-byte

produce an identical SHA-256 digest and thus the same `cacheKey`, e.g.:
- Request A: `GlProjectPath = "group/proj"`, `args = ["deadbeef"]`, `pathspecs = ["."]` → concatenation `"group/projdeadbeef."`
- Request B: `GlProjectPath = "group/projdead"`, `args = ["beef"]`, `pathspecs = ["."]` → concatenation `"group/projdeadbeef."`

Both yield the identical byte string `"group/projdeadbeef."` and hence the identical SHA-256 `cacheKey`, causing `s.archiveCache.Fetch` in `handleArchive` to treat these as the same cache entry and serve request B's `GetArchive` caller the archive bytes originally generated (and cached) for request A's repository/commit/path, once A's response has been cached — an unprivileged cross-repository content disclosure through the crafted `Repository.GlProjectPath` / `CommitId` / `Path` fields of the `GetArchive` RPC.

### Citations

**File:** internal/gitaly/service/repository/archive.go (L48-59)
```go
	path, err := storage.ValidateRelativePath(repoRoot, string(in.GetPath()))
	if err != nil {
		return structerr.NewInvalidArgument("%w", err)
	}

	exclude := make([]string, len(in.GetExclude()))
	for i, ex := range in.GetExclude() {
		exclude[i], err = storage.ValidateRelativePath(repoRoot, string(ex))
		if err != nil {
			return structerr.NewInvalidArgument("%w", err)
		}
	}
```

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

**File:** internal/gitaly/service/repository/archive.go (L244-251)
```go
	cacheKey := createArchiveCacheKey(repo.GetGlProjectPath(), args, pathspecs)
	_, _, err := s.archiveCache.Fetch(ctx, cacheKey, p.writer, func(writer io.Writer) error {
		archiveCommand, err := repo.Exec(ctx, gitcmd.Command{
			Name:        "archive",
			Flags:       []gitcmd.Option{gitcmd.ValueFlag{Name: "--format", Value: p.format}, gitcmd.ValueFlag{Name: "--prefix", Value: p.in.GetPrefix() + "/"}},
			Args:        args,
			PostSepArgs: pathspecs,
		}, gitcmd.WithEnv(env...), gitcmd.WithConfig(gitConfig...), gitcmd.WithSetupStdout())
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

**File:** internal/gitaly/service/repository/server.go (L47-47)
```go
	archiveCache          streamcache.Cache
```

**File:** internal/gitaly/service/repository/server.go (L70-70)
```go
		archiveCache:          deps.GetArchiveCache(),
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
