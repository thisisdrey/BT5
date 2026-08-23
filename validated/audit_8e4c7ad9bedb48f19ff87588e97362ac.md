### Title
Unbounded memory growth in `sendSearchFilesResultChunked` from single-line git grep matches without size limit - ([File: internal/gitaly/service/repository/search_files.go])

### Summary
`sendSearchFilesResultChunked` reads `git grep` output via `bufio.NewReader(cmd).ReadBytes('\n')` and accumulates matched lines into `buf` with no size cap until a `--\n` delimiter or EOF is seen. An attacker who pushes a repository containing a single extremely long line (with the search token but no embedded newline) can force both `ReadBytes` and the subsequent `buf = append(buf, line...)` to allocate memory proportional to that line's size before any bound is applied.

### Finding Description
`SearchFilesByContent` executes `git grep` with `--before-context`/`--after-context` and streams output through `sendSearchFilesResultChunked`, which reads line-by-line with `reader.ReadBytes('\n')` [1](#0-0) . Each non-delimiter line is appended to `buf` unconditionally, and `buf` is only flushed to the client (via `sendMatchInChunks`) when a literal `--\n` separator line is encountered or the process reaches EOF [2](#0-1) . `bufio.Reader.ReadBytes` has no maximum token size — unlike `bufio.Scanner` with `MaxScanTokenSize`, it will keep growing its internal buffer until the delimiter or EOF is found, so a single line without an embedded `\n` character is read/returned in full in one call, and then copied again into `buf`. Since `git grep` only emits a `--\n` separator between distinct match groups (not within a single matching line/hunk), a repository containing one file with an enormous single line matching the query produces one huge chunk that must be fully buffered in Gitaly's memory before `sendMatchInChunks` can stream it out. There is no check on `req.GetQuery()` length or a cap on accumulated match size in this path, so an unprivileged user who can push content to a repo they own and then call `SearchFilesByContent` on it controls the size of this allocation up to git's/Gitaly's own line-length limits (attacker can grow the file arbitrarily across many pushes/imports).

### Impact Explanation
This is a resource-exhaustion / denial-of-service issue: memory usage of the Gitaly RPC handler grows in proportion to the size of the single unbroken matching line (or contiguous match/context block) in the target file, independent of gRPC message chunking, because the buffering happens before any data is streamed to the client. Repeated or concurrent requests against such a crafted repository can drive up heap usage on the gitaly-server process, potentially leading to OOM and impacting availability for other tenants sharing the same Gitaly node — matching a DoS/resource-exhaustion impact class.

### Likelihood Explanation
An attacker only needs the ability to push a repository they own (a default, unprivileged capability) containing a file with a single very long line containing the search token, then issue a `SearchFilesByContentRequest` against it. No special configuration, credentials, or elevated role is required, and the RPC is part of normal repository search functionality reachable by any user with read access to the repo. The attack is fully repeatable and scalable by simply increasing the crafted line's size or firing concurrent requests.

### Recommendation
Bound the amount of unflushed match data buffered per match block (e.g., cap `buf` length and truncate/flush early or return an error once a configurable limit is exceeded), and/or switch to a streaming approach that flushes accumulated bytes to the client incrementally instead of waiting for the `--\n` delimiter, so memory usage stays bounded regardless of line length. Consider also using a bounded reader (e.g., `io.LimitReader` combined with chunked reads) instead of `ReadBytes('\n')` to avoid single-call unbounded allocation.

### Proof of Concept
Gittest-based Go test: create a repository with a blob containing one line of several hundred MB matching a trivial query (e.g., "a" repeated, ending without `\n`, git-grep-able), call `SearchFilesByContent` with that query, and measure heap growth (`runtime.MemStats`) during `sendSearchFilesResultChunked` execution — memory usage scales linearly with the crafted line size, whereas a bounded/streaming implementation should keep memory usage roughly constant regardless of match size.

### Citations

**File:** internal/gitaly/service/repository/search_files.go (L80-108)
```go
func sendSearchFilesResultChunked(cmd *command.Command, stream gitalypb.RepositoryService_SearchFilesByContentServer) error {
	var buf []byte
	reader := bufio.NewReader(cmd)

	for {
		line, err := reader.ReadBytes('\n')
		if err == io.EOF {
			break
		} else if err != nil {
			return fmt.Errorf("readbytes: %w", err)
		}

		if bytes.Equal(line, contentDelimiter) {
			if err := sendMatchInChunks(buf, stream); err != nil {
				return err
			}

			buf = nil
			continue
		}

		buf = append(buf, line...)
	}

	if len(buf) > 0 {
		return sendMatchInChunks(buf, stream)
	}

	return nil
```
