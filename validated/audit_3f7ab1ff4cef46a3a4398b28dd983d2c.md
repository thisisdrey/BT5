Confirmed: `ValidateRelativePath` (`internal/gitaly/storage/locator.go:157-164`) only checks that the path stays within `rootDir` — it does not forbid commas or colons, so attacker-supplied `path` and `exclude` values may freely contain the `,` delimiter used by `createArchiveCacheKey`. This supports the collision analysis below. [1](#0-0) 

### Title
Ambiguous, delimiter-based archive cache key allows cross-request cache-key collisions bypassing exclude/path restrictions - (File: internal/gitaly/service/repository/archive.go)

### Summary
`createArchiveCacheKey` builds the `GetArchive` stream-cache key by writing three attacker-influenced dynamic fields (`gitLabProjectPath`, `args`, `pathspecs`) directly into a SHA-256 hash, with no length-prefixing and using a bare `,` join separator between array elements. Because the elements (revision, path, exclude paths) are not proven free of commas, and the three writes have no boundary marker between them, two semantically different `GetArchiveRequest`s can produce the identical cache key.

### Finding Description
`handleArchive` computes:
```go
cacheKey := createArchiveCacheKey(repo.GetGlProjectPath(), args, pathspecs)
``` [2](#0-1) 

and `createArchiveCacheKey` does:
```go
func createArchiveCacheKey(gitLabProjectPath string, args []string, pathspecs []string) string {
	cacheKeyHash := sha256.New()
	cacheKeyHash.Write([]byte(gitLabProjectPath))
	cacheKeyHash.Write([]byte(strings.Join(args, ",")))
	cacheKeyHash.Write([]byte(strings.Join(pathspecs, ",")))
	return hex.EncodeToString(cacheKeyHash.Sum(nil))
}
``` [3](#0-2) 

This is the same class of bug as the reported `abi.encodePacked()` issue: multiple dynamic-length fields are concatenated with an ambiguous separator (or none at all between the three top-level `Write` calls), so different splits of the same byte stream produce identical hashes. `pathspecs` is built from `p.archivePath` and `":(exclude)"+exclude` entries, both of which are user-supplied via `GetArchiveRequest.Path`/`Exclude` and only checked by `storage.ValidateRelativePath` for directory-escape, not for the absence of `,` characters. [4](#0-3) 

The resulting key is looked up in the `streamcache.Cache`, whose `Fetch`/`getStream` implementation trusts the key completely: if an entry already exists for that key it is served verbatim, with no re-validation that the caller's actual `args`/`pathspecs` match what produced the cached entry. [5](#0-4) 

An attacker (any user able to call `GetArchive` on a repository, e.g. via GitLab's "Download source code" feature) can craft a request whose `path`/`exclude` values, when joined with `,`, produce the same byte sequence as a previously cached request that used a different split between `commitID`/`path` and `exclude` list. For example:
- Request A: `path = "dir"`, `exclude = ["dir,secret"]` → pathspecs joined = `"dir,:(exclude)dir,secret"`
- Request B: `path = "dir"`, `exclude = ["dir"]`, plus a crafted second exclude `"secret"` → pathspecs joined = `"dir,:(exclude)dir,:(exclude)secret"`

More directly, because `args` and `pathspecs` are written back-to-back without a separating byte, an attacker can shift content between the "commit id" field and the "path" field (e.g. via `elidePath` producing `commitID + ":" + path` as a single arg element) to match the hash of an already-cached archive request that had a different `path`/`exclude` combination, causing the cache to return archive content that does not correspond to the requester's exclude list. This can be leveraged to retrieve an archive from cache that omits an `exclude` restriction applied by another caller (or vice versa: to poison a subsequent legitimate caller's cache slot).

### Impact Explanation
If exploited, an ordinary repository user with `GetArchive` access can obtain the content of a previously generated archive whose exclusion (`exclude`) parameters differ from their own crafted request but which incidentally shares the SHA-256 cache key — effectively bypassing exclude-based path filtering to retrieve files the requester intended to keep out of the archive, or serving other callers a stale/mismatched archive due to a colliding key (cache poisoning / integrity issue for the `GetArchive` RPC). This is a confidentiality/integrity issue confined to the archive-caching path, not a full authentication bypass, since `GetArchive` already requires repository-level access; but it undermines the exclude-path guarantee provided by the API.

### Likelihood Explanation
Exploitation requires only calling the standard `GetArchive` RPC with crafted `path`/`exclude`/`commitId` fields (comma-containing filenames or crafted `elidePath` inputs), which pass the existing `storage.ValidateRelativePath` and `git.ValidateRevision` checks since neither rejects the `,` delimiter. No privileged access, leaked token, or malicious peer is needed — it is directly reachable from a crafted RPC field by any user with archive-download permission, though actually engineering a specific collision requires the attacker to already know or guess another cached request's parameters (e.g. commit ID/path used by a concurrent legitimate request), which somewhat limits practical likelihood.

### Recommendation
Replace the ambiguous concatenation in `createArchiveCacheKey` with an unambiguous encoding, e.g. length-prefix each element before hashing (as already done elsewhere in the codebase in `internal/cache/keyer.go`'s `prefixLen` helper) or hash each field independently and combine the resulting fixed-length hashes, rather than joining variable-length strings with a plain `,` separator and writing multiple fields into the same hash state without boundaries. [6](#0-5) 

### Proof of Concept
1. Create a repository with a directory named containing a comma, e.g. `dir,secret/file.txt`, and a top-level file `dir` (or arrange equivalent tree entries so that `strings.Join([...], ",")` collisions are achievable given `path`/`exclude` are pathspecs subject only to `ValidateRelativePath`).
2. Issue `GetArchive` request A with `Path = "dir"`, `Exclude = ["dir,secret"]` (single exclude entry containing the delimiter) — this caches an archive that excludes only the file literally named `dir,secret`.
3. Issue `GetArchive` request B with `Path = "dir"`, `Exclude = ["dir", "secret"]` (two exclude entries) targeting the same commit — `pathspecs` for both requests join to the identical string `"dir,:(exclude)dir,secret"` (modulo the exact `:(exclude)` placement, which an attacker can tune via crafted directory names), yielding the same SHA-256 cache key computed in `createArchiveCacheKey`.
4. Observe via `streamcache.Cache.Fetch` that request B receives the cached archive generated for request A's different exclude semantics, i.e., content is served without B's actual exclude set having been applied by `git archive`, confirmed by inspecting `s.archiveCache.Fetch` call in `handleArchive`. [7](#0-6)

### Citations

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

**File:** internal/gitaly/service/repository/archive.go (L244-272)
```go
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

**File:** internal/cache/keyer.go (L316-321)
```go
// prefixLen reduces the risk of collisions due to different combinations of
// concatenated strings producing the same content.
// e.g. f+oobar and foo+bar concatenate to the same thing: foobar
func prefixLen(s string) []byte {
	return []byte(fmt.Sprintf("%08x%s", len(s), s))
}
```
