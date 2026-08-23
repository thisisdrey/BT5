### Title
Unbounded memory/CPU consumption in `SearchFilesByName` via negative `Limit` (and no offset-based issue) - ([File: internal/gitaly/service/repository/search_files.go])

### Summary
`server.SearchFilesByName` forwards attacker-controlled `Offset`/`Limit` directly into `parseLsTree` without any validation or server-side ceiling. Because `parseLsTree` only caps accumulation when `limit > 0`, any non-positive `Limit` (0, or negative values like `-1`) disables the cap entirely, letting an attacker force the handler to buffer the full `ls-tree` output of a repository into memory in one unbounded `files` slice before a single response chunk is sent.

### Finding Description
`SearchFilesByName` calls `parseLsTree(objectHash, cmd, filter, int(req.GetOffset()), int(req.GetLimit()))` with the raw `int32` request fields converted to `int`, with no validation performed in `validateSearchFilesRequest` (`internal/gitaly/service/repository/search_files.go:174-192`) — it only checks the repository, `Query`, and `Ref`, never `Offset` or `Limit`.

Inside `parseLsTree`:
```go
index++
if index > offset {
    files = append(files, path)
}
if limit > 0 && len(files) >= limit {
    break
}
```
(`internal/gitaly/service/repository/search_files.go:211-217`)

The loop-termination guard `limit > 0 && len(files) >= limit` is only active for strictly positive `limit`. Supplying `Limit = -1` (or any value `<= 0`) makes the condition permanently false, so the loop runs until `parser.NextEntryPath()` returns `io.EOF` — i.e., until the entire `ls-tree -r -z` output for the given ref has been parsed and appended to `files`. There is no independent server-side maximum entry count or byte budget; the only bound comes from the client-supplied `Limit`, which the client can simply omit or set negative to defeat.

Regarding `Offset`: it is compared only as `index > offset`, a plain integer comparison, never used to index into a slice or array. A negative offset simply makes `index > offset` true from the first entry (equivalent to `Offset = 0`), and there is no overflow risk since the `int32` value is widened to platform `int` (64-bit) before use, so there is no wraparound. Offset therefore does not introduce any additional indexing bug beyond bypassing pagination semantics — it does not compound the DoS beyond what `Limit <= 0` already causes.

The entire response is also built as a single in-memory `[][]byte` and sent in one `stream.Send` call (`internal/gitaly/service/repository/search_files.go:165`) rather than streamed incrementally, which is what turns unbounded accumulation into an unbounded memory allocation on the Gitaly node.

### Impact Explanation
An unprivileged user who can trigger `SearchFilesByName` against a repository they control (e.g., own or fork) can set `Limit` to `0` or any negative int32 and force Gitaly to enumerate and buffer every file path in the tree for the given ref into memory, and to hold the git `ls-tree` subprocess open until completion. On a repository with a very large tree, this causes disproportionate memory and CPU consumption on the Gitaly node, i.e., a Denial-of-Service of the RPC handler / server resource exhaustion, matching GitLab's "Denial of Service" bounty impact class for a shared Gitaly node.

### Likelihood Explanation
Trivial and fully repeatable: the attacker only needs a repository (their own, forked, or imported) with a large file tree and the ability to call the public `SearchFilesByName` gRPC method with `Limit` set to `0` or a negative number — no special role, secret, or non-default configuration required.

### Recommendation
Enforce an absolute server-side cap in `parseLsTree` (or before calling it) independent of client input — e.g., clamp `limit` to a fixed maximum when `limit <= 0` or `limit` exceeds the maximum, and reject/clamp negative `Offset` values in `validateSearchFilesRequest`. Additionally, consider streaming results incrementally rather than accumulating the whole `[][]byte` before a single `stream.Send`.

### Proof of Concept
```go
func TestParseLsTree_NegativeLimitBypassesCap(t *testing.T) {
    // Build a cmd producing N entries (N large, e.g. 100000) via ls-tree.
    files, err := parseLsTree(objectHash, cmd, nil, 0, -1)
    require.NoError(t, err)
    // Fails today: no hard cap enforced, len(files) == N regardless of the
    // negative Limit; a maximum should be enforced (e.g. len(files) <= maxAllowedEntries).
    require.LessOrEqual(t, len(files), maxAllowedEntries)
}
```
Equivalent RPC-level PoC: call `SearchFilesByName` with `Offset: 0, Limit: -1` (or `0`) against a repository with a large tree and observe full unbounded result accumulation and elevated memory usage on the Gitaly server. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** internal/gitaly/service/repository/search_files.go (L160-166)
```go
	files, err := parseLsTree(objectHash, cmd, filter, int(req.GetOffset()), int(req.GetLimit()))
	if err != nil {
		return err
	}

	return stream.Send(&gitalypb.SearchFilesByNameResponse{Files: files})
}
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
