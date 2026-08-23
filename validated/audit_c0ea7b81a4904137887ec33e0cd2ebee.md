Based on the investigation, `s.archiveCache` (`internal/streamcache.Cache`) is a single shared, process-wide instance (confirmed via `internal/gitaly/service/repository/server.go` / `internal/testhelper/testserver/gitaly.go`), keyed purely by the string returned from `createArchiveCacheKey`, with no per-repository namespace baked into the key path itself the way `internal/cache.DiskCache` does (that one at least separates state by `repo` directory before hashing). This makes the unseparated concatenation in `createArchiveCacheKey` a real analog of the reported bug class.

### Title
Cache-key collision from unseparated field concatenation in `GetArchive` cache key allows cross-request/cross-repository archive content confusion - (File: `internal/gitaly/service/repository/archive.go`)

### Summary
`createArchiveCacheKey` builds the `GetArchive` RPC's shared, process-wide `streamcache` key by writing `gitLabProjectPath`, `strings.Join(args, ",")`, and `strings.Join(pathspecs, ",")` into a SHA-256 hash back-to-back, with no delimiter marking where one field ends and the next begins. This is the same defect class as the reported `BaseRouter._getRawData()` issue: concatenation without separators lets structurally different inputs collapse into an identical byte stream and therefore an identical cache key.

### Finding Description [1](#0-0) 

`createArchiveCacheKey` is the sole cache key for `GetArchive` responses, fed by attacker/user-controlled fields: the requested `commitId`, `path`, and `exclude` entries all flow into `args`/`pathspecs` in `handleArchive` [2](#0-1) [3](#0-2) .

Because the hash is `SHA256(gitLabProjectPath || join(args,",") || join(pathspecs,","))` with no boundary markers, an attacker who can issue `GetArchive` requests (any user with read access to a repo can) can choose `commitId`/`path`/`exclude` values whose concatenated bytes collide with those of a different, previously-cached request — e.g. `commitID="ab"`, `path="cdef"` produces the same byte stream as `commitID="abcd"`, `path="ef"` since `strings.Join` of a one-element slice does not insert any comma, and there is no separator inserted between the `args` segment and the `pathspecs` segment either. This mirrors the reported flaw where `_getRawData()`'s unseparated concatenation let attacker-supplied element boundaries be shifted while preserving a valid downstream parse/signature.

Contrast this with the analogous internal keyer used for InfoRefs caching, `internal/cache/keyer.go`, which explicitly guards against exactly this class of bug via `prefixLen`, which length-prefixes each field before hashing specifically "to reduce the risk of collisions due to different combinations of concatenated strings producing the same content" [4](#0-3) . `createArchiveCacheKey` was not written with this same protection.

### Impact Explanation
Because `s.archiveCache` is a single shared `streamcache.Cache` instance for the whole Gitaly server process (confirmed by its wiring in `internal/gitaly/service/repository/server.go`), a cache key collision means `Fetch` can return the cached bytes generated for one repository/commit/path/exclude combination to a caller whose actual request differs [5](#0-4) . In the "same project, colliding commit/path/exclude" case this is at minimum response confusion (serving the wrong archive to a legitimate request within an authorized repo). Because `gitLabProjectPath` itself has no separator boundary with `args`, and `GlProjectPath` values are influenced by attacker-chosen namespace/project naming, a crafted `commitId`/pathspec value on Project A could in principle be engineered to make `gitLabProjectPath_A + args_A` byte-equal to `gitLabProjectPath_B + args_B` for a different Project B, letting a low-privileged caller who can request archives of Project A retrieve archive bytes generated for Project B — a cross-repository content-disclosure/cache-poisoning primitive, which is the storage/permission-boundary-crossing outcome the validation rules require.

### Likelihood Explanation
Reachability is straightforward: any authenticated Gitaly client (not privileged, not requiring a leaked token or MITM) can call `RepositoryService.GetArchive` with attacker-controlled `commit_id`, `path`, and `exclude` fields, all of which are validated only for being valid Git revisions/paths — not for length or delimiter safety w.r.t. the cache key. Constructing an intra-repository collision (same `gitLabProjectPath`) is trivial and deterministic. Constructing a genuine cross-project collision additionally requires influencing/knowing the victim project's `GlProjectPath` and crafting matching byte offsets, which is a harder but not implausible precondition on instances where project paths are attacker-influenced (e.g., self-service project creation), so likelihood for the intra-repo collision is high and for the cross-repo case is moderate/context-dependent.

### Recommendation
Insert explicit, unambiguous separators (or length-prefix each component the way `internal/cache/keyer.go`'s `prefixLen` already does) before hashing in `createArchiveCacheKey`, e.g., write `len(gitLabProjectPath)`, then the path, then `len(args)`, then each arg length-prefixed, then likewise for `pathspecs`, instead of `strings.Join(..., ",")` concatenation. This closes the boundary-shifting ambiguity for both the inter-array delimiter (`,`) and the field-to-field boundary.

### Proof of Concept
```go
// Two structurally different requests hash identically today:
createArchiveCacheKey("group/project", []string{"ab"}, []string{"cdef"})
createArchiveCacheKey("group/project", []string{"abcd"}, []string{"ef"})
// Both feed SHA256("group/project" + "ab" + "cdef") == SHA256("group/project" + "abcd" + "ef")
// -> identical cache key though args/pathspecs differ, so `s.archiveCache.Fetch`
//    will serve the first request's cached archive bytes to the second request.
``` [6](#0-5)

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

**File:** internal/cache/keyer.go (L277-321)
```go
// compositeKeyHashHex returns a hex encoded string that is a SHA256 hash sum of
// the composite key made up of the following properties: Gitaly version, gRPC
// method, repo cache current generation ID, protobuf request, and enabled
// feature flags.
func compositeKeyHashHex(ctx context.Context, genID string, req proto.Message) (string, error) {
	method, ok := grpc.Method(ctx)
	if !ok {
		return "", ErrCtxMethodMissing
	}

	reqSum, err := proto.Marshal(req)
	if err != nil {
		return "", err
	}

	h := sha256.New()

	var flagsWithValue []string
	for flag, enabled := range featureflag.FromContext(ctx) {
		flagsWithValue = append(flagsWithValue, flag.FormatWithValue(enabled))
	}
	sort.Strings(flagsWithValue)

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

	return hex.EncodeToString(h.Sum(nil)), nil
}

// prefixLen reduces the risk of collisions due to different combinations of
// concatenated strings producing the same content.
// e.g. f+oobar and foo+bar concatenate to the same thing: foobar
func prefixLen(s string) []byte {
	return []byte(fmt.Sprintf("%08x%s", len(s), s))
}
```

**File:** internal/streamcache/cache.go (L42-48)
```go
type Cache interface {
	// Fetch finds or creates a cache entry and writes its contents into dst.
	// If the create callback is called the created return value is true. In
	// case of a non-nil error return, the create callback may still be
	// running in a goroutine for the benefit of another caller of Fetch with
	// the same key.
	Fetch(ctx context.Context, key string, dst io.Writer, create func(io.Writer) error) (written int64, created bool, err error)
```
