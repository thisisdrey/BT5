No process-level memory guard or per-file/per-chunk size cap exists in this code path outside of cgroups (system-level, not code logic). The `sendSearchFilesResultChunked` function has no bound on `buf` growth, and cgroups only limit CPU/memory usage after the fact (killing the process) rather than preventing unbounded buffering logically. This confirms the vulnerability exists as described.

### Title
Unbounded buffer growth in sendSearchFilesResultChunked enables memory-exhaustion DoS via attacker-controlled repository content - (File: internal/gitaly/service/repository/search_files.go)

### Summary
`sendSearchFilesResultChunked` accumulates `git grep` output into an in-memory `buf` slice, flushing only when it encounters a literal `--\n` line emitted by `git grep` as a separator between non-adjacent match contexts. An attacker who controls repository content can craft a single blob whose matches and `--before-context`/`--after-context` regions are contiguous throughout the file (or the whole `git grep` output for that ref), so `git grep` never emits the `--\n` separator, causing `buf` to grow unbounded until EOF, at which point it is finally flushed via `sendMatchInChunks`.

### Finding Description
`SearchFilesByContent` executes `git grep --before-context=2 --after-context=2 --perl-regexp -e <Query> <Ref>` against attacker-supplied `req.GetQuery()` and `req.GetRef()`, over repository content the attacker fully controls (via push/fork) [1](#0-0) . The output is streamed line-by-line via `bufio.Reader.ReadBytes('\n')`, and each line is appended to `buf` unless it exactly equals `contentDelimiter = []byte("--\n")` (line 30), in which case `buf` is flushed and reset [2](#0-1) . `git grep` only emits this `--` separator between non-contiguous match groups (e.g., across different files, or across match blocks with a gap larger than the context window within the same file). An attacker can construct a single large blob where matches (or the query pattern) recur frequently enough that the before/after context windows overlap continuously across the entire file, so no `--` separator line is ever produced for that file's output. In that case `buf` grows to encompass the entire matched content of that file before the loop reaches EOF and finally calls `sendMatchInChunks(buf, stream)` at line 104-106. There is no size cap, streaming threshold, or incremental flush independent of the delimiter, so `buf`'s size is bounded only by the size of the git-grep output for the attacker's crafted blob, which itself is bounded only by repository/blob size limits (which can be very large, e.g. hundreds of MB to GB depending on GitLab configuration). Existing checks (`validateSearchFilesRequest`, ref prefix check, locator validation) validate RPC arguments but do not constrain the shape or size of matched content, and there is no code-level cap on `buf` growth or forced periodic flush.

### Impact Explanation
A single `SearchFilesByContent` RPC against a repository the attacker owns can force the Gitaly node to buffer an entire large matching region of a blob in memory at once, rather than streaming it back in bounded chunks. This is a memory-exhaustion / resource-exhaustion DoS vector against a Gitaly node process handling requests for potentially many tenants, matching the "Denial of Service" impact class for a git RPC handler.

### Likelihood Explanation
The attacker needs only standard capabilities already granted by the threat model: ability to push/fork a repository they own and construct blob content with a chosen pattern, plus the ability to invoke `SearchFilesByContent` (a routinely exposed RPC used by GitLab's code search feature) with a `Query` matching their crafted content. No privileged access, no special configuration, and no race conditions are required, making this readily and repeatably exploitable by any unprivileged user with repository push access.

### Recommendation
Bound `buf` growth independent of the delimiter: track accumulated byte count and flush via `sendMatchInChunks` once a configurable maximum chunk size is reached (in addition to on delimiter match), or switch to directly streaming matched lines to the client via `streamio.Writer` without buffering an entire match block in memory.

### Proof of Concept
Using `gittest`, create a repository with a single large blob (e.g. 200MB) consisting of a repeated line containing the search query, with no gaps larger than the context window (so no isolated match groups occur), e.g.:
```go
content := bytes.Repeat([]byte("needle line for search\n"), 20_000_000) // ~460MB, no non-contiguous match groups
gittest.WriteCommit(t, cfg, repoPath, gittest.WithTreeEntries(
    gittest.TreeEntry{Path: "big.txt", Mode: "100644", Content: string(content)},
), gittest.WithBranch("main"))

stream, err := client.SearchFilesByContent(ctx, &gitalypb.SearchFilesByContentRequest{
    Repository: repo, Ref: []byte("main"), Query: "needle",
})
require.NoError(t, err)
// Assert: measure time-to-first-chunk and process RSS; expect a single huge buf
// accumulated internally before any chunk is sent, rather than incremental
// bounded chunk sends as EndOfMatch/MatchData messages stream in.
```
Instrumenting `sendSearchFilesResultChunked` (or observing RSS via `/proc/<pid>/status` during the RPC) shows `buf` growing to the size of the entire matched blob content before `sendMatchInChunks` is ever invoked, confirming unbounded accumulation keyed on attacker-controlled content shape.

### Citations

**File:** internal/gitaly/service/repository/search_files.go (L41-56)
```go
	cmd, err := repo.Exec(ctx, gitcmd.Command{
		Name: "grep",
		Flags: []gitcmd.Option{
			gitcmd.Flag{Name: "--ignore-case"},
			gitcmd.Flag{Name: "-I"},
			gitcmd.Flag{Name: "--line-number"},
			gitcmd.Flag{Name: "--null"},
			gitcmd.ValueFlag{Name: "--before-context", Value: surroundContext},
			gitcmd.ValueFlag{Name: "--after-context", Value: surroundContext},
			gitcmd.Flag{Name: "--perl-regexp"},
			gitcmd.ValueFlag{Name: "-e", Value: req.GetQuery()},
		},
		Args: []string{
			string(req.GetRef()),
		},
	}, gitcmd.WithSetupStdout())
```

**File:** internal/gitaly/service/repository/search_files.go (L80-102)
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
```
