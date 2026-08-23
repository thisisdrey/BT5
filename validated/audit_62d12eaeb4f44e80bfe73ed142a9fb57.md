### Title
Unbounded per-entry `git check-attr` round-trips in `linguist.Instance.Stats` allow CPU/time exhaustion via oversized attacker-controlled tree - ([File: internal/gitaly/linguist/linguist.go])

### Summary
`linguist.Instance.Stats` iterates every blob entry returned by `git-ls-tree(1)` (full recalculation path) or `git-diff-tree(1)` (incremental path) and, for each entry, calls `newFileInstance` which performs a synchronous `CheckAttrCmd.Check()` round-trip against a single long-lived `git check-attr --stdin` subprocess. There is no cap on the number of entries processed per request, so an attacker who controls repository content (via push/fork/import) can force `CommitLanguages` to process millions of tree entries, each incurring a blocking write/flush/read cycle over a pipe to the check-attr subprocess.

### Finding Description
`CommitLanguages` (`internal/gitaly/service/commit/languages.go:34`) resolves a commit and calls `linguist.New(...).Stats(ctx, commitID)` [1](#0-0) . Inside `Stats`, when a full recalculation is required (any first-time computation, cache invalidation, or stale language names), it builds a `gitpipe.LsTree` iterator with `LsTreeWithRecursive()` and `LsTreeWithBlobFilter()` over the whole tree [2](#0-1) . Both the `skipFunc` inside the ls-tree/diff-tree pipeline and the main iteration loop call `newFileInstance(filename, checkAttr)`, which internally calls `checkAttr.Check(path)` [3](#0-2) .

`CheckAttrCmd.Check` (`internal/git/gitattributes/check_attr.go:63-107`) writes the path to the subprocess's stdin, flushes, and then blocking-reads `c.count` NUL-delimited triples from stdout before returning [4](#0-3) . This happens once per tree entry — including entries that are ultimately excluded — with no batching, no entry limit, and no early-exit/backpressure mechanism tied to entry count. `CommitLanguages` is a mutator RPC (it persists cache and casts a transaction vote), so it is not subject to the read-only pressure/circuit-breaking mechanisms described in `doc/load-management-architecture.md`, and no RPC-specific concurrency or cost limiter is applied specifically to bound tree size.

An attacker with ordinary push/fork/import rights can create a commit whose tree contains an arbitrarily large number of blob entries (e.g., zero-byte files), which is not restricted by any existing repository-content validation (`storage.ValidateRelativePath`, `git.ValidateRevision`, etc., only validate paths/revisions of the request, not the size of pushed tree objects). Once pushed, any caller invoking `CommitLanguages` against that commit ID (which needs no special privilege beyond read access, and is triggered automatically by GitLab Rails on repository browsing) forces the full recalculation path, driving `Stats` into O(N) synchronous IPC round-trips with N unbounded by the request itself.

### Impact Explanation
This causes sustained CPU consumption and long RPC hang time on the Gitaly worker proportional to the attacker-chosen tree size, with each `check-attr` invocation consuming a full worker goroutine and OS process for the duration of the RPC. Because `check-attr` is a single long-lived subprocess per request that must be driven serially (single mutex, sequential write-then-read protocol), there's no parallelism to absorb the cost, and other requests sharing the Gitaly node's CPU/proc budget can be degraded — a DoS of the RPC handler and a resource-exhaustion class impact per GitLab's bounty program.

### Likelihood Explanation
Preconditions are minimal: any unprivileged user who can push, fork, or import a repository they control can shape a tree with a very large number of blob entries, then trigger `CommitLanguages` (invoked routinely by GitLab Rails when viewing a project, e.g., after a push webhook or project page load) with a fresh commit ID that forces full recalculation (`needsFullRecalculation` returns true whenever there is no prior cached commit, which is the default state for a newly pushed commit) [5](#0-4) . This makes the issue easily and repeatably triggerable without any elevated access.

### Recommendation
Introduce a per-request cap on the number of tree entries (and/or a wall-clock/CPU budget) processed by `linguist.Instance.Stats` during full recalculation, rejecting or truncating with a clear error (e.g., `FailedPrecondition`/`ResourceExhausted`) once a configurable threshold is exceeded. Consider batching `check-attr` queries instead of issuing one synchronous round-trip per file, and/or applying the existing cost-aware/concurrency-limiter framework (`doc/load-management-architecture.md`) to `CommitLanguages` so that large trees increase its RPC cost score and can be rate-limited or preempted under load.

### Proof of Concept
```go
// internal/gitaly/linguist/linguist_bench_test.go
func BenchmarkStatsLargeTree(b *testing.B) {
    cfg := testcfg.Build(b)
    repo, repoPath := gittest.CreateRepository(b, testhelper.Context(b), cfg)

    // Build a tree with N zero-byte blobs.
    const N = 1_000_000
    var treeEntries []gittest.TreeEntry
    for i := 0; i < N; i++ {
        treeEntries = append(treeEntries, gittest.TreeEntry{
            Mode: "100644", Path: fmt.Sprintf("file%d.txt", i), Content: "",
        })
    }
    commitID := gittest.WriteCommit(b, cfg, repoPath, gittest.WithTreeEntries(treeEntries...))

    logger := testhelper.NewLogger(b)
    catfileCache := catfile.NewCache(cfg)
    localRepo := localrepo.New(logger, gittest.NewCommandFactory(b, cfg), cfg, repo)

    start := time.Now()
    _, err := linguist.New(cfg, logger, catfileCache, localRepo).Stats(testhelper.Context(b), commitID)
    require.NoError(b, err)
    b.Logf("Stats() over %d entries took %s", N, time.Since(start))
    // Expected: elapsed time grows linearly (or worse) with N and with no
    // configurable cap, demonstrating unbounded CPU time for a single RPC call.
}
```
Run with increasing `N` (e.g., 10^4, 10^5, 10^6) to show linear/unbounded growth in wall-clock time with no cap or early rejection, confirming the DoS.

### Citations

**File:** internal/gitaly/service/commit/languages.go (L50-58)
```go
	commitID, err := s.lookupRevision(ctx, repo, revision)
	if err != nil {
		return nil, structerr.NewInternal("looking up revision: %w", err)
	}

	stats, err := linguist.New(s.cfg, s.logger, s.catfileCache, repo).Stats(ctx, git.ObjectID(commitID))
	if err != nil {
		return nil, structerr.NewInternal("language stats: %w", err)
	}
```

**File:** internal/gitaly/linguist/linguist.go (L109-129)
```go
	if full || staleNames {
		stats = newLanguageStats()

		skipFunc := func(result *gitpipe.RevisionResult) (bool, error) {
			f, err := newFileInstance(string(result.ObjectName), checkAttr)
			if err != nil {
				return true, fmt.Errorf("new file instance: %w", err)
			}

			// Skip files that are an excluded filetype based on filename.
			return f.IsExcluded(), nil
		}

		// Full recalculation is needed, so get all the files for the
		// commit using git-ls-tree(1).
		revlistIt = gitpipe.LsTree(ctx, inst.repo,
			commitID.String(),
			gitpipe.LsTreeWithRecursive(),
			gitpipe.LsTreeWithBlobFilter(),
			gitpipe.LsTreeWithSkip(skipFunc),
		)
```

**File:** internal/gitaly/linguist/linguist.go (L177-186)
```go
	for objectIt.Next() {
		object := objectIt.Result()
		filename := string(object.ObjectName)

		f, err := newFileInstance(filename, checkAttr)
		if err != nil {
			return nil, fmt.Errorf("linguist new file instance: %w", err)
		}

		lang, size, err := f.DetermineStats(object)
```

**File:** internal/gitaly/linguist/linguist.go (L216-231)
```go
func (inst *Instance) needsFullRecalculation(ctx context.Context, cachedID, commitID git.ObjectID) (bool, error) {
	if cachedID == "" {
		return true, nil
	}

	// The cached commit may no longer exist after a force-push, GC/repack, or
	// repository restore/move. Peeling with `^{commit}` ensures the object is
	// specifically a commit, not just any object type (blob, tree, or tag)
	exists, err := inst.repo.HasRevision(ctx, git.Revision(cachedID.String()+"^{commit}"))
	if err != nil {
		return true, fmt.Errorf("linguist: verifying cached commit: %w", err)
	}
	if !exists {
		inst.logger.WithField("cached_commit_id", cachedID.String()).InfoContext(ctx, "linguist: cached commit no longer exists, forcing full recalculation")
		return true, nil
	}
```

**File:** internal/git/gitattributes/check_attr.go (L68-104)
```go
	c.m.Lock()
	defer c.m.Unlock()

	if _, err := c.stdin.WriteString(path + "\000"); err != nil {
		return nil, fmt.Errorf("write path: %w", err)
	}
	if err := c.stdin.Flush(); err != nil {
		return nil, fmt.Errorf("flush: %w", err)
	}

	attrs := Attributes{}
	buf := make([]string, 0, 3)

	// Using git-check-attr(1) with -z will return data in the format:
	// <path> NUL <attribute> NUL <info> NUL ...
	for i := 0; i < c.count; {
		word, err := c.stdout.ReadBytes('\000')
		if err != nil {
			return nil, fmt.Errorf("read line: %w", err)
		}

		buf = append(buf, string(bytes.TrimSuffix(word, []byte{0})))

		if len(buf) < 3 {
			continue // Keep going until we have 3 words
		}

		if buf[0] != path {
			return nil, fmt.Errorf("wrong path name detected, expected %q, got %q", path, buf[0])
		}
		if buf[2] != Unspecified {
			attrs = append(attrs, Attribute{Name: buf[1], State: buf[2]})
		}

		i++
		buf = buf[:0]
	}
```
