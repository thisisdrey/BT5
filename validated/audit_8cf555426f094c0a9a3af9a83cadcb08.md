### Title
Unbounded memory allocation in `parseLsTree` when `Limit=0` allows DoS via large repo tree - ([File: internal/gitaly/service/repository/search_files.go])

### Summary
`SearchFilesByName` calls `parseLsTree` which accumulates every matching `ls-tree` entry path into an in-memory `files [][]byte` slice before any bound is applied. When the caller sets `Limit=0` (the proto default/zero value), the `if limit > 0 && len(files) >= limit` break condition never triggers, so the entire tree listing is buffered in memory and only sent in a single `stream.Send` call after `ls-tree` finishes streaming.

### Finding Description
In `SearchFilesByName` (internal/gitaly/service/repository/search_files.go:111-166), the request's `Offset` and `Limit` fields are taken directly from attacker-controlled RPC input with no validation, other than filter length capping via `searchFilesFilterMaxLength` (only applies to the regex `Filter`, not to `Limit`/`Offset`). The `ls-tree -r -z` command is spawned against `req.GetRef()` and `req.GetQuery()` and its output is parsed via `localrepo.NewParser(cmd, objectHash).NextEntryPath()` inside `parseLsTree` (lines 194-221).

The loop in `parseLsTree` appends every entry path to `files` once `index > offset`, and only stops early `if limit > 0 && len(files) >= limit`. Because protobuf's zero value for an unset int is `0`, `Limit=0` is functionally "no limit," causing the loop to run until `NextEntryPath` returns `io.EOF`, i.e., until the entire (possibly attacker-inflated) tree has been read and buffered in `files`. Only after the loop completes does `SearchFilesByName` call `stream.Send` once with the full `Files` slice (line 165) — there is no chunked streaming and no memory cap enforced by `validateSearchFilesRequest` (lines 174-192), which checks only for non-empty `Query`/`Ref` and a `-`-prefixed ref, not path count or limit bounds.

An attacker who owns/forks a repository can push a tree with millions of tiny/empty blob entries (trivially producible, e.g., via `git mktree`/`git commit-tree` scripting) and then invoke `SearchFilesByName` with an empty `Filter` and `Limit=0` against that ref, forcing the Gitaly node serving that repository to allocate O(N) `[]byte` path slices and hold the entire result in memory until the `ls-tree` process completes and the single large response is sent.

### Impact Explanation
This is a resource-exhaustion / DoS-of-RPC-handler issue matching GitLab's "Denial of Service" bounty impact class. A single unprivileged, self-owned repository can force one Gitaly RPC invocation to allocate memory proportional to the number of tracked paths in the tree with no upper bound, which can exhaust available memory on the Gitaly node/storage shard and stall or crash the process, degrading service for other tenants (repos) sharing that storage/partition. The response itself is also unbounded and would eventually exceed gRPC message size limits, but the memory exhaustion happens before that check is even reached.

### Likelihood Explanation
Highly feasible and repeatable: the attacker only needs push access to a repository they own or a fork (standard GitLab capability), the ability to construct a tree with many entries (a simple scripted `git mktree`/`git fast-import` push, no special git server features needed), and the ability to call the `SearchFilesByName` RPC with `Limit=0` (the default value when unset, requiring no special crafting). No admin/operator privilege, secret, or non-default configuration is required.

### Recommendation
Enforce a hard maximum number of returned entries in `parseLsTree` regardless of the caller-supplied `Limit` (e.g., treat `Limit<=0` as "use a fixed internal maximum" rather than "unlimited"), and/or stream results back to the client in chunks (similar to `sendSearchFilesResultChunked` used for `SearchFilesByContent`) instead of buffering the full `Files` slice before a single `stream.Send`. Additionally, validate `Limit`/`Offset` in `validateSearchFilesRequest` to reject or clamp unreasonable values.

### Proof of Concept
```go
func TestParseLsTree_UnboundedMemoryWithZeroLimit(t *testing.T) {
    // Build a repo/tree with a very large number of entries (e.g. 1_000_000
    // empty blobs) via git mktree / fast-import, then run:
    cmd, err := repo.Exec(ctx, gitcmd.Command{
        Name: "ls-tree",
        Flags: []gitcmd.Option{
            gitcmd.Flag{Name: "--full-tree"},
            gitcmd.Flag{Name: "--name-status"},
            gitcmd.Flag{Name: "-r"},
            gitcmd.Flag{Name: "-z"},
        },
        Args: []string{ref},
        PostSepArgs: []string{""},
    }, gitcmd.WithSetupStdout())
    require.NoError(t, err)

    var m1, m2 runtime.MemStats
    runtime.ReadMemStats(&m1)

    files, err := parseLsTree(objectHash, cmd, nil /* filter */, 0 /* offset */, 0 /* limit */)
    require.NoError(t, err)

    runtime.ReadMemStats(&m2)
    t.Logf("heap grew by %d bytes for %d files", m2.HeapAlloc-m1.HeapAlloc, len(files))

    // Expected (currently failing) assertion: memory/allocation should be
    // bounded by an internal maximum regardless of Limit=0.
    require.LessOrEqual(t, len(files), someInternalMaxCap)
}
```
Running this against a repo with a sufficiently large tree demonstrates linear, unbounded heap growth with `len(files)` equal to the full entry count, confirming no per-request cap is enforced. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** internal/gitaly/service/repository/search_files.go (L160-165)
```go
	files, err := parseLsTree(objectHash, cmd, filter, int(req.GetOffset()), int(req.GetLimit()))
	if err != nil {
		return err
	}

	return stream.Send(&gitalypb.SearchFilesByNameResponse{Files: files})
```

**File:** internal/gitaly/service/repository/search_files.go (L174-192)
```go
func validateSearchFilesRequest(ctx context.Context, locator storage.Locator, req searchFilesRequest) error {
	if err := locator.ValidateRepository(ctx, req.GetRepository()); err != nil {
		return err
	}

	if len(req.GetQuery()) == 0 {
		return errors.New("no query given")
	}

	if len(req.GetRef()) == 0 {
		return errors.New("no ref given")
	}

	if bytes.HasPrefix(req.GetRef(), []byte("-")) {
		return errors.New("invalid ref argument")
	}

	return nil
}
```

**File:** internal/gitaly/service/repository/search_files.go (L194-221)
```go
func parseLsTree(objectHash git.ObjectHash, cmd *command.Command, filter *regexp.Regexp, offset int, limit int) ([][]byte, error) {
	var files [][]byte
	var index int
	parser := localrepo.NewParser(cmd, objectHash)

	for {
		path, err := parser.NextEntryPath()
		if err != nil {
			if errors.Is(err, io.EOF) {
				break
			}
			return nil, err
		}
		if filter != nil && !filter.Match(path) {
			continue
		}

		index++
		if index > offset {
			files = append(files, path)
		}
		if limit > 0 && len(files) >= limit {
			break
		}
	}

	return files, nil
}
```
