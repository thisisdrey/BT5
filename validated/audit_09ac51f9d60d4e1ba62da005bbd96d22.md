### Title
Archive response cache keyed by client-supplied `GlProjectPath` instead of the physical repository identity - ([File: internal/gitaly/service/repository/archive.go])

### Summary
`GetArchive` builds its cache key from the caller-supplied `GlProjectPath` metadata field plus command args/pathspecs, rather than from the repository's actual physical identity (storage name + relative path). Since `GlProjectPath` is an arbitrary, unverified field on the `Repository` proto message, two different physical repositories can be made to collide in the shared archive cache, mirroring the reported bug class ("tracked by a proxy identifier instead of the real pair/identity").

### Finding Description
`handleArchive` computes the cache key as: [1](#0-0) 

`createArchiveCacheKey` hashes only the GitLab project path, git-archive args, and pathspecs — it never incorporates the repository's storage name or relative path, which are the values actually used to locate the on-disk repository: [2](#0-1) 

`GlProjectPath` is simply a field carried on the incoming `Repository` message (set by the caller, e.g. GitLab Rails or any authenticated client issuing the RPC) and is not cross-checked against the `storage_name`/`relative_path` pair that the `Locator` uses to resolve the real repository on disk (contrast with `locator.go` where `storage_name`+`relative_path` is the actual physical key). This is the same bug class as the oracle report: the cache assumes `GlProjectPath` is a canonical, unique identifier for "the repository," when in fact it is caller-controlled data that has no enforced 1:1 relationship with the physical repository being archived. The `s.archiveCache` is a single server-wide cache instance (constructed in `server.go` and shared across all `GetArchive` calls for all repositories served by that Gitaly node), so a collision in the derived key returns a cached stream generated from a *different* physical repository.

### Impact Explanation
If an attacker (or a misconfigured/legacy caller) can set `GlProjectPath` on the `Repository` message of their own request to match the `GlProjectPath` used for another repository hosted on the same Gitaly storage/node, and can produce the same `args`/`pathspecs` (e.g. same `commit_id` and same or empty path/exclude set — trivially achievable with well-known commit IDs like the initial empty-tree commit, or fresh empty repositories), the archive cache will serve them the archived content generated for the *other* repository. This is a cross-repository data disclosure primitive: content from one repository's archive is exposed to a party requesting a different repository, entirely through a client-controlled metadata field rather than through the actual storage/relative-path repository identity that authorization is supposed to be scoped to.

### Likelihood Explanation
Reachable directly from an ordinary `GetArchive` RPC call — no privileged access, hook execution, or malicious peer is required, only a valid gRPC request with a crafted `GlProjectPath` field, which is attacker/caller-supplied per request and not validated for uniqueness or tied to the actual repository. The likelihood of an exact `args`/`pathspecs` collision is the main constraint, but this is trivially satisfiable for empty/initial repositories or well-known commit references, and once combined with a matching `GlProjectPath` it produces a deterministic cache hit across repositories.

### Recommendation
Include the repository's actual physical identity (storage name and relative path from the `Locator`, i.e. the same fields used for on-disk resolution) as part of `createArchiveCacheKey`, instead of relying on the caller-supplied `GlProjectPath`. This mirrors the C4 recommendation of keying by the true unique pair rather than a proxy value that different logical entities can share.

### Proof of Concept
1. Create repository A on a storage and issue `GetArchive` with `Repository{storage_name: "default", relative_path: "a.git", gl_project_path: "shared/path"}`, `commit_id = <empty-tree-commit-A>`, default path — populates the archive cache under `hash("shared/path" + [commit_id] + [pathspec])`.
2. Create a separate repository B with different actual content, and issue `GetArchive` with `Repository{storage_name: "default", relative_path: "b.git", gl_project_path: "shared/path"}` and the same `commit_id`/pathspec combination (achievable if both repositories share a well-known/empty root commit, or if a caller is free to pick `gl_project_path` and coordinate commit IDs).
3. Because `createArchiveCacheKey` ignores `storage_name`/`relative_path`, the second request receives the cached archive stream produced for repository A rather than generating a fresh archive of repository B.

### Citations

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
