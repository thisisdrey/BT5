### Title
Archive cache key collision via client-controlled `gl_project_path` enables cross-repository content disclosure - ([File: internal/gitaly/service/repository/archive.go])

### Summary
`GetArchive` derives its content-addressed cache key exclusively from the client-supplied `GlProjectPath` field plus request arguments (commit ID, format, pathspecs), never from the repository's actual `StorageName`/`RelativePath`. Because `GlProjectPath` is unauthenticated, client-controlled metadata that Gitaly never validates against the physical repository being accessed, two different physical repositories can be made to resolve to the identical cache key, exactly mirroring the SEDA `requestId` bug class where a hash lacking a caller/entity-bound and unique component allows collisions and hijacking of another party's request slot.

### Finding Description
`GetArchive` builds the repository object from the client-supplied `*gitalypb.Repository` message and computes the cache key like this: [1](#0-0) 

```go
repo := s.localRepoFactory.Build(p.in.GetRepository())
cacheKey := createArchiveCacheKey(repo.GetGlProjectPath(), args, pathspecs)
_, _, err := s.archiveCache.Fetch(ctx, cacheKey, p.writer, func(writer io.Writer) error {
```

and: [2](#0-1) 

The key is `sha256(GlProjectPath || args || pathspecs)`. `args` is derived from `commitID`, `format`/compression flags, and `prefix`, none of which are bound to the actual `StorageName`/`RelativePath` of the repository. `GlProjectPath` itself is a free-form string on the `Repository` proto that Gitaly never checks for correspondence to the repository actually resolved on disk — only `StorageName`/`RelativePath` are validated via `locator.ValidateRepository`/`repo.Path(ctx)`: [3](#0-2) 

Since none of the storage/relative-path identity of the repository participates in the cache key, this is functionally identical to the SEDA `deriveRequestId` bug: the identifier used to gate/re-use a shared resource lacks a component that uniquely and non-spoofably ties it to the caller's actual object (here, the physical repository), leaving it open to collision by any client who can supply an arbitrary `gl_project_path`.

### Impact Explanation
An attacker who has push/read access to any repository on the same Gitaly node can pick an arbitrary `gl_project_path` value that matches (or that they cause to later match) a victim repository's `gl_project_path`. If the attacker also knows or can guess a commit ID that will later be requested for the victim repo with the same archive format/prefix/pathspecs, the attacker can pre-populate `s.archiveCache` with content taken from their own (attacker-controlled) repository under that colliding key. When the victim (or GitLab acting on the victim's behalf) subsequently calls `GetArchive` on the real target repository with matching commit ID/format/pathspecs, Gitaly's `streamcache.Fetch` returns the previously cached — attacker-supplied — archive bytes instead of re-executing `git archive` against the real repository, resulting in cross-repository content confusion/disclosure. This is a concrete escape of the repository storage boundary via cache-key collision, not merely a stale-cache correctness bug.

### Likelihood Explanation
Exploitation requires the attacker to control the `gl_project_path` field of a `GetArchiveRequest.Repository` for a repo they can legitimately access, and to predict (or already know) the exact `commit_id`/format/pathspec combination that a victim will subsequently request for the target repository. `commit_id` is often knowable (e.g., a specific tag or branch head visible via other channels, or coordinated timing with CI archive requests), and `gl_project_path` may be inferable from a project's namespace/path. This is a moderate-likelihood, low-complexity collision because the cache key formula intentionally omits the one authoritative, server-verified identifier (`StorageName`/`RelativePath`) that would have prevented collision across repositories.

### Recommendation
Bind the archive cache key to the resolved, server-verified repository identity rather than client-supplied metadata: include the repository's `StorageName` and validated `RelativePath` (or an internal repo ID) in `createArchiveCacheKey`, in addition to (or instead of) `GlProjectPath`. This mirrors the SEDA fix of adding a caller-bound, non-spoofable component (there, `msg.sender`/nonce; here, the server-resolved repository path) to the derived identifier.

### Proof of Concept
1. Attacker has access to `RepoA` (`StorageName=default`, `RelativePath=attacker.git`) and knows victim `RepoB` has `gl_project_path = "victim-group/victim-project"` and expects an archive request for `commit_id = X`, `format = TAR_GZ`, no exclude/prefix.
2. Attacker calls `GetArchive` against `RepoA` with `Repository.gl_project_path` forged to `"victim-group/victim-project"` and `commit_id = X`, causing `createArchiveCacheKey("victim-group/victim-project", [X], [])` to be populated in `s.archiveCache` with `RepoA`'s tarball content.
3. Later, GitLab/victim calls `GetArchive` against the real `RepoB` with the same `commit_id`/format — the identical cache key is computed from `RepoB.GetGlProjectPath()`/args, and `s.archiveCache.Fetch` returns the cached (attacker-supplied) bytes from step 2 instead of executing `git archive` on `RepoB`. [2](#0-1)

### Citations

**File:** internal/gitaly/service/repository/archive.go (L34-46)
```go
func (s *server) GetArchive(in *gitalypb.GetArchiveRequest, stream gitalypb.RepositoryService_GetArchiveServer) error {
	ctx := stream.Context()
	repository := in.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repository); err != nil {
		return structerr.NewInvalidArgument("%w", err)
	}
	compressArgs, format := parseArchiveFormat(in.GetFormat())
	repo := s.localRepoFactory.Build(repository)

	repoRoot, err := repo.Path(ctx)
	if err != nil {
		return err
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
