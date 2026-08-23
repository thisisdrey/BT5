### Title
Archive cache key collision via unescaped comma-joined `args`/`pathspecs` allows cross-request content confusion - (File: `internal/gitaly/service/repository/archive.go`)

### Summary
`createArchiveCacheKey` builds the on-disk `archiveCache` lookup key by writing the GitLab project path, the joined `args`, and the joined `pathspecs` into a SHA-256 hash with no length-prefixing or escaping between fields. Because pathspecs/commit revisions are attacker-influenced strings that may themselves contain commas, and because there is no delimiter between the three writes, two semantically distinct `GetArchive` requests (different commit IDs, paths, or exclude lists) can be engineered to produce an identical cache key. This lets one request's response be served for a different request — the same bug class as the reported `RentPayload` issue, where a payload/key that should uniquely identify one operation is reusable across multiple distinct ones.

### Finding Description
`GetArchive` builds a cache key purely from concatenated strings: [1](#0-0) 

`args` contains the (attacker-supplied) `CommitId` (optionally combined with `archivePath` via `:`), and `pathspecs` contains the `archivePath` and `:(exclude)<path>` entries built directly from client-controlled `GetArchiveRequest.Path` / `GetArchiveRequest.Exclude` fields (validated only for path traversal, not for character content): [2](#0-1) 

`strings.Join(args, ",")` and `strings.Join(pathspecs, ",")` are written to the hash back-to-back with no separator between the project path, args blob, and pathspecs blob, and no escaping of commas embedded in the individual path components (git allows filenames containing commas). This means two different `(args, pathspecs)` tuples can serialize to the identical byte sequence fed to SHA-256, producing the same `cacheKey` used by `s.archiveCache.Fetch`: [3](#0-2) 

The codebase is aware of exactly this bug class elsewhere and has already mitigated it in the mutator-response disk cache keyer, which explicitly length-prefixes each component before hashing to avoid concatenation ambiguity: [4](#0-3) 

`createArchiveCacheKey` was not given the same treatment, so the analogous collision is reachable directly from an ordinary `GetArchive` RPC call issued against a repository the caller has read access to (crafted `commit_id`/`path`/`exclude` fields), without any privileged actor, leaked token, or MITM being required.

### Impact Explanation
If an attacker (or even an unprivileged legitimate user racing another user's request) can cause a hash collision between the cache key for their crafted request and the cache key of an already-cached (or soon-to-be-cached) archive for a *different* commit/path/exclude combination within the same repository, `archiveCache.Fetch` will serve the pre-existing cached data instead of generating a fresh archive for the actual request. This is a concrete cross-request content-confusion bug: a user can receive archive content that does not correspond to the commit/path/exclusions they requested, and conversely can poison the cache so that other legitimate callers requesting the correct combination are served the attacker's colliding archive. This falls into the "cross-repository/cross-request object access via cache/key collision" bucket, directly analogous to the replay bug class in the report (a value meant to uniquely bind a request/response pair can be reused/forced to collide across distinct legitimate operations).

### Likelihood Explanation
Exploitability depends on the attacker's ability to control filenames/pathspecs containing commas (achievable by anyone who can push commits to the target repository, since Git permits commas in filenames) and on timing/existence of a previously-cached entry with a colliding key, or two concurrent requests to the same repository. This requires read access to the target repository (an ordinary, unprivileged actor) but does require crafting specific inputs and is bounded to callers of `GetArchive`, so likelihood is moderate rather than trivial.

### Recommendation
Apply the same defense already used in `internal/cache/keyer.go`'s `compositeKeyHashHex`/`prefixLen`: length-prefix (or otherwise unambiguously delimit) each component (`gitLabProjectPath`, each element of `args`, each element of `pathspecs`) before writing it into the hash, instead of naively joining with `,` and writing three unseparated blobs. This ensures the cache key uniquely and unambiguously identifies the requested archive parameters.

### Proof of Concept
1. Create a repository containing two files whose names, when the archive request's `args`/`pathspecs` are joined with `,`, produce colliding serialized byte sequences — e.g., request A: `path="a,b"`, exclude=[] vs. request B: `path="a"`, exclude=["b"]. Both produce `pathspecs = ["a,b"]` vs `pathspecs = ["a", ":(exclude)b"]`; by choosing filenames that contain literal commas and colon-exclude markers, an attacker can align the joined strings so `strings.Join(pathspecs, ",")` is byte-identical between two distinct legitimate `GetArchiveRequest` payloads for the same `commit_id` and project path.
2. Issue request A (archives `a,b` as a single path). This populates `archiveCache` under key `H(glProjectPath || "commitID" || "a,b")`.
3. Issue request B (archives path `a`, excluding `b`) with the same `commit_id`, chosen so that `strings.Join(args,",")+strings.Join(pathspecs,",")` matches request A's concatenation exactly.
4. Observe that request B is served the cached archive content generated for request A (containing `b` unexcluded, and lacking the actual pathspec semantics of request B), confirming cross-request content confusion via cache-key collision.

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

**File:** internal/gitaly/service/repository/archive.go (L242-245)
```go
	repo := s.localRepoFactory.Build(p.in.GetRepository())

	cacheKey := createArchiveCacheKey(repo.GetGlProjectPath(), args, pathspecs)
	_, _, err := s.archiveCache.Fetch(ctx, cacheKey, p.writer, func(writer io.Writer) error {
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
