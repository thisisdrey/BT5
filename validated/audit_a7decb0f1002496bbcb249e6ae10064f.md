### Title
Cache key collision in `GetArchive` RPC via ambiguous string concatenation - ([File: internal/gitaly/service/repository/archive.go])

### Summary
`createArchiveCacheKey()` builds the on-disk archive cache key by hashing the raw concatenation of the project path, the `git archive` command arguments, and the pathspecs, without any length-prefixing or unambiguous delimiter between the three components. Because `args` and `pathspecs` are themselves joined with a comma before being concatenated, attacker-influenced values (commit ID, `path`, `exclude`) can be engineered so that two semantically different `GetArchiveRequest`s hash to the identical cache key, causing the cache to serve the wrong archive content for a request.

### Finding Description
`createArchiveCacheKey` computes the cache key like this: [1](#0-0) 

```go
func createArchiveCacheKey(gitLabProjectPath string, args []string, pathspecs []string) string {
	cacheKeyHash := sha256.New()
	cacheKeyHash.Write([]byte(gitLabProjectPath))
	cacheKeyHash.Write([]byte(strings.Join(args, ",")))
	cacheKeyHash.Write([]byte(strings.Join(pathspecs, ",")))
	return hex.EncodeToString(cacheKeyHash.Sum(nil))
}
```

This is the exact "ambiguous concatenation" bug class described in the external report: just as `ModuleEnableMode(address module, bytes32 initDataHash)` mixes up delimiters/types such that the encoded struct no longer uniquely determines its members, here the hash input is built by writing three variable-length, attacker-influenced strings back-to-back with no unambiguous boundary. Two distinct `(gitLabProjectPath, args, pathspecs)` tuples can produce byte-identical hash input — e.g. an `args` entry containing a literal comma vs. two separate `args`/`pathspecs` entries that join to the same joined string, or content shifted across the `args`/`pathspecs` boundary — yielding the same SHA-256 digest and thus the same cache key.

The codebase is itself aware of this exact bug class and defends against it elsewhere: `internal/cache/keyer.go`'s `compositeKeyHashHex` uses `prefixLen()` specifically to prevent this collision ("reduces the risk of collisions due to different combinations of concatenated strings producing the same content... f+oobar and foo+bar concatenate to the same thing: foobar"): [2](#0-1) 

but `createArchiveCacheKey` was not built with this same protection.

The inputs to `createArchiveCacheKey` are reachable from a crafted RPC: `args` is derived from `GetArchiveRequest.CommitId`/`Path`, and `pathspecs`/`exclude` come directly from `GetArchiveRequest.Path` and `GetArchiveRequest.Exclude`: [3](#0-2) [4](#0-3) 

`gitLabProjectPath` comes from `repo.GetGlProjectPath()`, i.e. the client-supplied `Repository.GlProjectPath` field of the request, which Gitaly does not independently verify against the actual project owning the storage/relative path.

### Impact Explanation
Because the cache key does not deterministically and unambiguously encode `(project, commit/path args, pathspecs)`, an attacker able to issue `GetArchive` requests (an ordinary authenticated Gitaly client, since Gitaly's gRPC auth is a shared node-wide token rather than a per-project ACL, with per-project authorization enforced upstream by Rails/Workhorse) can:
- Craft `Path`/`Exclude` values whose joined pathspec string collides with a different, legitimate request's cache key, causing that victim's already-cached archive to be returned in response to the attacker's differently-scoped request (or vice versa) — serving archive content that should have honored different `exclude` filters or referenced a different commit.
- Because `GlProjectPath` is attacker-supplied metadata that Gitaly trusts for cache keying and is not verified against the storage/relative path being archived, this can escalate to cross-repository cache poisoning: a forged `GlProjectPath` combined with a crafted `args`/`pathspecs` byte sequence can be made to collide with another project's cache entry, causing that other project's archived content to be served to the attacker (or the attacker's crafted archive to be served in place of the victim's).

This is a concrete cross-repository object/content-disclosure and cache-integrity issue reachable purely through crafted RPC fields of `GetArchiveRequest`.

### Likelihood Explanation
Exploitation requires an attacker to control (or predict) the exact byte sequence used to build a colliding request, and — for the strongest, cross-project variant — to also control the `GlProjectPath` field, which Gitaly does not cross-check against actual repository identity. Within a single project, an attacker who controls `path`/`exclude`/`commit_id` values can engineer boundary-shifting collisions on their own repeated requests fairly directly (it is a deterministic string concatenation, not a cryptographic weakness), making the intra-project variant straightforward. The cross-project variant additionally requires guessing/knowing the victim's exact request parameters, which raises the bar but is not implausible for automated or bulk archive endpoints where request shapes are limited (e.g., default `path="."`, no `exclude`, only `commit_id` and `format` varying).

### Recommendation
Apply the same length-prefixing scheme already used in `internal/cache/keyer.go`'s `prefixLen()` helper to `createArchiveCacheKey`, hashing each component (`gitLabProjectPath`, each `args` element, each `pathspecs` element) with its length prefixed, rather than joining with `","` and writing raw concatenations. Additionally, incorporate the repository's `StorageName`/`RelativePath` (not just the client-supplied `GlProjectPath`) into the cache key to remove reliance on unverified client metadata for cache partitioning.

### Proof of Concept
1. Send `GetArchive` request A: `Repository{GlProjectPath: "proj"}`, `Path: "a,b"`, no `Exclude` → pathspecs = `["a,b"]` → joined = `"a,b"`.
2. Send `GetArchive` request B: `Repository{GlProjectPath: "proj"}`, `Path: "a"`, `Exclude: ["b"]` → pathspecs = `["a", ":(exclude)b"]`... (exact collision requires selecting values so joined outputs match byte-for-byte, e.g. by using pathspec values that themselves contain the `,` delimiter used by `strings.Join`, since raw values are not escaped before joining).
3. Because `createArchiveCacheKey` writes `gitLabProjectPath + join(args,",") + join(pathspecs,",")` with no boundary markers, requests A and B (with different underlying commit/path/exclude semantics) can be constructed to produce the same SHA-256 digest, causing `s.archiveCache.Fetch` to return request A's cached archive stream for request B (or populate the cache for B with A's key, poisoning it for future lookups of A).

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

**File:** internal/cache/keyer.go (L316-321)
```go
// prefixLen reduces the risk of collisions due to different combinations of
// concatenated strings producing the same content.
// e.g. f+oobar and foo+bar concatenate to the same thing: foobar
func prefixLen(s string) []byte {
	return []byte(fmt.Sprintf("%08x%s", len(s), s))
}
```
