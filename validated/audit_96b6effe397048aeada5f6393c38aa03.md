### Title
Delimiter-less Cache Key Concatenation in `createArchiveCacheKey` Enables Archive Cache Collision/Poisoning - (File: `internal/gitaly/service/repository/archive.go`)

### Summary
`RepositoryService.GetArchive` derives its response-cache key by hashing several attacker-influenced request fields (`args` and `pathspecs`, built from `commit_id`, `path`, `exclude`, and `elide_path`) with `createArchiveCacheKey`. The function concatenates variable-length string slices using `strings.Join(..., ",")` and writes each joined blob to the hash sequentially with no length-prefixing or delimiter between the `args` blob and the `pathspecs` blob (nor within a slice when an individual element itself contains a comma). This is the same root cause pattern as the reported `VaultFactory` weak-salt bug: a derived identifier is generated from multiple caller-controlled parameters without unambiguous framing, so two semantically different requests can produce an identical key.

### Finding Description
`createArchiveCacheKey` is defined as: [1](#0-0) 

It is invoked in `handleArchive` with values built directly from client-supplied `GetArchiveRequest` fields (`CommitId`, `Path`, `Exclude`, `ElidePath`): [2](#0-1) [3](#0-2) 

Because `strings.Join(args, ",")` and `strings.Join(pathspecs, ",")` are hashed back-to-back with no separator between the two blobs, and because individual `args`/`pathspecs` elements (e.g. `commitId`, `commitId+":"+path`, or `":(exclude)"+exclude`) can themselves legally contain the `,` character, an attacker can construct two distinct `(args, pathspecs)` pairs that serialize to the identical byte stream fed into `sha256.New()`. For example, `args=["X,Y"], pathspecs=["Z"]` and `args=["X"], pathspecs=["Y,Z"]` both produce the joined bytes `X,YZ`... more directly, since there's no separator *between* the args-join and pathspecs-join at all, `args=["A"], pathspecs=["BC"]` and `args=["AB"], pathspecs=["C"]` both hash to `"AB C"` → `"ABC"`, an exact collision. `commit_id` is validated only via `git.ValidateRevision`, which permits many characters, and `path`/`exclude` are validated only to be within the repository (`storage.ValidateRelativePath`), not restricted against comma or restructuring characters.

The resulting `cacheKey` is passed to `s.archiveCache.Fetch`, which streams whatever content is already cached for that key back to the requester instead of regenerating the archive: [4](#0-3) 

### Impact Explanation
An attacker with the ability to send `GetArchive` requests for a repository can craft `commit_id`/`path`/`exclude` values whose concatenation collides with a different, previously-cached request for the same project (or shortly-thereafter cached request, if generated first). Instead of receiving the freshly generated archive for the commit/path/exclude combination they actually asked for, they are served the previously cached archive of a colliding request. This breaks the integrity guarantee of the archive cache and can be used to poison the cache: a low-privileged caller can pre-populate a colliding key with attacker-chosen content, causing a subsequent legitimate caller requesting a different (but colliding) commit/path/exclude combination to receive the attacker-poisoned archive instead of the archive for the content they actually requested — a direct cross-request content-hijack of the `GetArchive` RPC, analogous to the vault "hijacking" scenario in the reference report (wrong content served under a caller-predictable identifier due to weak parameter framing).

### Likelihood Explanation
`GetArchive` is a routinely used, unprivileged-relative RPC that any client with repository read access can call, and the colliding fields (`commit_id`, `path`, `exclude`) are fully attacker-controlled with only path-traversal/revision-syntax validation, not delimiter-safety validation. No race condition or privileged position is required — the attacker simply needs to submit two crafted requests (or one crafted request following a legitimate one) targeting the same project.

### Recommendation
Build the cache key using a collision-resistant, unambiguous encoding, e.g. hash each element with its length prefixed (or hash each element separately and hash the concatenation of digests), instead of joining variable-length elements with a fixed separator character that can appear in the elements themselves. For example, write `len(item)` and `item` for every entry in `args` and `pathspecs` before mixing categories, ensuring no combination of inputs can produce an identical byte stream.

### Proof of Concept
Given `createArchiveCacheKey(gitLabProjectPath, args, pathspecs)` from `internal/gitaly/service/repository/archive.go` lines 290-295, the following two calls produce the same SHA-256 digest:
```go
createArchiveCacheKey("group/project", []string{"A"}, []string{"BC"})
createArchiveCacheKey("group/project", []string{"AB"}, []string{"C"})
```
Both write the same underlying byte sequence `"group/project" + "A" + "BC"` == `"group/project" + "AB" + "C"` == `"group/projectABC"` to the hash, yielding an identical `cacheKey`. Mapping this to real `GetArchive` requests: two requests with different `commit_id`/`path`/`exclude` combinations whose joined `args`/`pathspecs` strings coincide (e.g. one request's `commit_id` containing a literal comma matching the split point of another request's `path`/`exclude` list) will be served each other's cached archive content via `s.archiveCache.Fetch` at `internal/gitaly/service/repository/archive.go` lines 244-245.

### Citations

**File:** internal/gitaly/service/repository/archive.go (L195-213)
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

**File:** internal/gitaly/service/repository/archive.go (L242-272)
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
			return err
		}

		if len(p.compressArgs) > 0 {
			command, err := command.New(ctx, s.logger, p.compressArgs,
				command.WithStdin(archiveCommand), command.WithStdout(writer),
			)
			if err != nil {
				return err
			}

			if err := command.Wait(); err != nil {
				return err
			}
		} else if _, err = io.Copy(writer, archiveCommand); err != nil {
			return err
		}

		return archiveCommand.Wait()
	})
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
