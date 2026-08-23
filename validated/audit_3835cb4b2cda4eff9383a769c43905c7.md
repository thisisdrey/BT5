### Title
Unbounded PCRE regex in SearchFilesByContent enables catastrophic-backtracking DoS - (File: internal/gitaly/service/repository/search_files.go)

### Summary
`SearchFilesByContent` passes `req.GetQuery()` directly into `git grep --perl-regexp -e <query>` with no length or complexity limit, unlike its sibling `SearchFilesByName`, which explicitly caps filter length via `searchFilesFilterMaxLength` and compiles with Go's linear-time RE2 engine. Because `--perl-regexp` uses a backtracking PCRE engine, an attacker-supplied pattern with nested quantifiers can trigger catastrophic backtracking against matching content, pinning a `git grep` worker and consuming CPU indefinitely with no complexity check to reject it beforehand.

### Finding Description
In `SearchFilesByContent` [1](#0-0) , the query is inserted unescaped as `gitcmd.ValueFlag{Name: "-e", Value: req.GetQuery()}` alongside `gitcmd.Flag{Name: "--perl-regexp"}`. The only validation performed is `validateSearchFilesRequest`, which merely checks that the query and ref are non-empty and that the ref doesn't start with `-` [2](#0-1)  — it does not bound query length or reject pathological regex constructs.

By contrast, `SearchFilesByName` explicitly guards against this class of issue: it rejects filters longer than `searchFilesFilterMaxLength` (1000 chars) and compiles the filter with Go's `regexp` package (RE2, guaranteed linear-time, no backtracking) [3](#0-2) . `SearchFilesByContent` has no equivalent safeguard, and it deliberately uses `--perl-regexp`, which invokes a PCRE-style backtracking engine capable of exponential-time matching on adversarial patterns like `(a+)+$` against long non-matching lines.

There is no RPC-level timeout enforced by Gitaly itself; the underlying `git grep` process only terminates when the gRPC context is cancelled (e.g., client disconnect or explicit deadline) or when it naturally finishes. An attacker who simply omits a deadline can let the command run unbounded, exhausting worker/goroutine and CPU resources on the node while other tenants using the same storage suffer degraded service. General backpressure mechanisms like `[[concurrency]]` limits [4](#0-3)  only bound the number of concurrent RPCs per repo/RPC — they do nothing to cap the CPU cost of a single request already admitted.

### Impact Explanation
An attacker with a valid but unprivileged token can craft a repository (which they control the content and size of, e.g., via a fork/import) containing long lines and submit a single `SearchFilesByContent` call with a catastrophic-backtracking pattern. This pins a `git grep` process indefinitely on CPU, consuming a Gitaly command slot and CPU core for the duration, and can be repeated to exhaust available resources — a DoS of the Gitaly node affecting all repositories/tenants sharing that storage shard. This matches GitLab's "Denial of Service" bounty impact class for resource exhaustion of a shared backend service.

### Likelihood Explanation
Feasibility is high: the attacker needs only a valid gRPC token (already normal for any authenticated GitLab user hitting Gitaly through Workhorse/Rails) and the ability to control or name a repository with sizeable matching content, both trivially available to any unprivileged user via push/fork/import. The RPC is reachable through GitLab's code-search feature paths that call `SearchFilesByContent`. Repeatability is straightforward — a single crafted request per worker slot suffices, and it can be repeated to saturate multiple slots.

### Recommendation
Apply the same protections used for `SearchFilesByName` to `SearchFilesByContent`: enforce a maximum query length (e.g., reuse or introduce a constant similar to `searchFilesFilterMaxLength`), and consider avoiding `--perl-regexp` backtracking semantics in favor of a bounded/linear-time engine, or impose a hard wall-clock/CPU timeout on the spawned `git grep` process (context deadline set server-side rather than relying solely on the client-supplied gRPC deadline). Additionally, consider basic regex complexity heuristics (e.g., rejecting nested unbounded quantifiers) before invoking `git grep`.

### Proof of Concept
```go
func TestSearchFilesByContent_CatastrophicBacktracking(t *testing.T) {
    // Set up a repo with a file containing a long line of 'a's with no trailing match
    // e.g. strings.Repeat("a", 40) + "!"  committed to HEAD.
    req := &gitalypb.SearchFilesByContentRequest{
        Repository: repo,
        Ref:        []byte("HEAD"),
        Query:      "(a+)+$", // catastrophic backtracking pattern
    }
    start := time.Now()
    stream, err := client.SearchFilesByContent(ctx, req)
    require.NoError(t, err)
    for {
        _, err := stream.Recv()
        if err == io.EOF {
            break
        }
        require.NoError(t, err)
    }
    elapsed := time.Since(start)
    // Baseline benign query (e.g. "abc") completes in milliseconds;
    // adversarial query is expected to hang far longer with no
    // server-enforced cap, demonstrating absence of a complexity/time limit.
    t.Logf("elapsed: %s", elapsed)
}
```
Compare wall-clock time against a benign query of similar length (e.g. `abc`) to show the disproportionate, attacker-controlled CPU cost with no enforced ceiling.

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

**File:** internal/gitaly/service/repository/search_files.go (L118-128)
```go
	var filter *regexp.Regexp
	if req.GetFilter() != "" {
		if len(req.GetFilter()) > searchFilesFilterMaxLength {
			return structerr.NewInvalidArgument("filter exceeds maximum length")
		}
		var err error
		filter, err = regexp.Compile(req.GetFilter())
		if err != nil {
			return structerr.NewInvalidArgument("filter did not compile: %w", err)
		}
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

**File:** doc/backpressure.md (L15-24)
```markdown
## Concurrency queue

Limit the number of concurrent RPCs that are in flight on each Gitaly node for each
repository per RPC using `[[concurrency]]` configuration:

```toml
[[concurrency]]
rpc = "/gitaly.SmartHTTPService/PostUploadPackWithSidechannel"
max_per_repo = 1
```
```
