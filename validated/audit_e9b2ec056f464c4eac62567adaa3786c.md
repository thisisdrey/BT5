### Title
Regular Expression Denial of Service via `SearchFilesByContent`'s unbounded PCRE query passed to `git grep --perl-regexp` - (File: internal/gitaly/service/repository/search_files.go)

### Summary
`SearchFilesByContent` takes the attacker-controlled `SearchFilesByContentRequest.Query` field and passes it verbatim as the pattern argument (`-e`) to `git grep --perl-regexp`, invoking Git's PCRE-based regex engine. Unlike Go's standard `regexp` package (RE2 automaton, guaranteed linear-time matching), PCRE supports backtracking constructs that are vulnerable to the exact bug class described in the Rails `Money` report (ambiguous, overlapping quantified groups causing quadratic/exponential match time). Because the query is unbounded in length/complexity and is matched against arbitrary repository blob content of arbitrary size, a crafted request can force the spawned `git grep` process into catastrophic backtracking, consuming CPU for a long time and tying up an RPC worker/goroutine.

### Finding Description [1](#0-0) 

The handler builds the `git grep` command directly from `req.GetQuery()`:
```go
gitcmd.Flag{Name: "--perl-regexp"},
gitcmd.ValueFlag{Name: "-e", Value: req.GetQuery()},
```
with `Args: []string{string(req.GetRef())}`, run against the full repository tree at the given ref. There is no evident cap on the complexity or structure of `Query` before it reaches PCRE — the only length-limiting constant found in this file, `searchFilesFilterMaxLength = 1000`, is documented as limiting a *filter* regex (matched with Go's safe `regexp` engine, e.g. in `SearchFilesByName`), not the `Query` field consumed by `--perl-regexp`. [2](#0-1) 

Note: I was not able to fully inspect `validateSearchFilesRequest` (only its call site was visible), so I cannot rule out that some additional validation exists on `Query` outside the excerpt reviewed; this should be verified in the actual source before treating this as certain, given index size limits may have truncated the file.

The rest of Gitaly's own regexes (e.g. `internal/git/reference_backend.go`, `internal/gitaly/diff/diff.go`, `internal/log/url_sanitizer.go`) are compiled with Go's `regexp` package, which is backed by RE2 and is immune to the catastrophic-backtracking class of bug described in the Rails report — these are not viable analogs. `git grep --perl-regexp` is the one place where an attacker-supplied pattern reaches a backtracking engine (PCRE), making it the closest structural analog to the reported vulnerability class.

### Impact Explanation
An attacker with push/read access sufficient to call `SearchFilesByContent` (a standard, unprivileged Gitaly RPC used by GitLab's code search feature) can submit a PCRE pattern engineered for catastrophic backtracking (e.g., patterns with nested/overlapping quantifiers analogous to `\D*[\d,]+`). Matched against large blobs in the repository, this can pin a CPU core for a long time inside the spawned `git grep` process, degrading or denying service for that Gitaly node (RPC-handler resource exhaustion).

### Likelihood Explanation
Likelihood is moderate-to-high: the RPC is reachable by any user with read access to the repository through ordinary GitLab code-search functionality, requires no special privileges, and the query field is fully attacker-controlled with no evidence of length or complexity restriction before being handed to a backtracking regex engine.

### Recommendation
- Cap the length/complexity of `Query` before passing it to `git grep --perl-regexp`, similar to `searchFilesFilterMaxLength` used elsewhere.
- Consider enforcing a wall-clock timeout on the spawned `git grep` process independent of the overall RPC context, so a pathological pattern cannot indefinitely consume CPU.
- Evaluate whether `--perl-regexp` (PCRE) is necessary versus using Git's basic/extended POSIX regex mode or Go-side validation/pre-screening for known-dangerous constructs (nested quantifiers, overlapping alternations).

### Proof of Concept
Send a `SearchFilesByContentRequest` with:
- `Repository`: any accessible repo containing at least one large text blob at `Ref`.
- `Query`: a PCRE pattern crafted for catastrophic backtracking, e.g. `(a|aa)+b` or `(\d+)+@`, matched against a large blob containing a long run of the ambiguous character(s) without the required terminator (so the engine must exhaust all backtracking permutations before failing).
- `Ref`: pointing at the blob's revision.

Observe that the spawned `git grep --perl-regexp -e '<pattern>' <ref>` process consumes CPU for a duration that scales super-linearly with input size, exceeding what similarly-sized benign queries take, until either it finishes or the RPC's deadline is exceeded — repeated concurrent requests amplify the impact on the node's overall throughput.

### Citations

**File:** internal/gitaly/service/repository/search_files.go (L22-28)
```go
const (
	surroundContext = "2"

	// searchFilesFilterMaxLength controls the maximum length of the regular
	// expression to thwart excessive resource usage when filtering
	searchFilesFilterMaxLength = 1000
)
```

**File:** internal/gitaly/service/repository/search_files.go (L32-59)
```go
func (s *server) SearchFilesByContent(req *gitalypb.SearchFilesByContentRequest, stream gitalypb.RepositoryService_SearchFilesByContentServer) error {
	ctx := stream.Context()

	if err := validateSearchFilesRequest(ctx, s.locator, req); err != nil {
		return structerr.NewInvalidArgument("%w", err)
	}

	repo := s.localRepoFactory.Build(req.GetRepository())

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
	if err != nil {
		return structerr.NewInternal("cmd start failed: %w", err)
	}
```
